import os
import json
import asyncio
import datetime
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.api.message_components import At, Plain

# 数据文件名
DATA_FILE = "birthday_data.json"

@register("astrbot_plugin_birthday", "Zhalslar_Assistant", "智能生日纪念日祝福", "2.1.0")
class BirthdayPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # 1. 初始化数据存储路径 (使用标准 StarTools)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_birthday")
        self.data_path = self.data_dir / DATA_FILE
        self.data = self._load_data()
        
        # 2. 启动定时任务
        self.last_check_date = None
        self._task = asyncio.create_task(self._scheduler_loop())

    async def terminate(self):
        """插件卸载时的清理工作"""
        logger.info("[Birthday] 正在停止插件...")
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[Birthday] 插件已停止。")

    # ================== 数据管理区域 ==================

    def _load_data(self):
        """加载数据，如果不存在则返回默认结构"""
        if not self.data_path.exists():
            return {"birthdays": [], "anniversaries": []}
        try:
            with open(self.data_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[Birthday] 数据加载失败: {e}")
            return {"birthdays": [], "anniversaries": []}

    def _save_data(self):
        """保存数据到 JSON 文件"""
        try:
            if not self.data_dir.exists():
                self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.data_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[Birthday] 数据保存失败: {e}")

    def _add_record(self, record_type, data):
        """
        通用的添加记录方法
        record_type: "birthdays" 或 "anniversaries"
        """
        target_list = self.data[record_type]
        
        # 如果是生日，先删除该用户在本群的旧记录 (覆盖更新)
        if record_type == "birthdays":
            self.data[record_type] = [
                x for x in target_list 
                if not (x["user_id"] == data["user_id"] and x["group_id"] == data["group_id"])
            ]
        
        # 添加新记录
        self.data[record_type].append(data)
        self._save_data()

    # ================== 核心指令区域 ==================
    
    @filter.command_group("bd")
    def bd(self):
        """生日助手指令组"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bd.command("scan")
    async def scan_group(self, event: AstrMessageEvent):
        """
        [管理员] 扫描当前群成员资料
        注意：仅支持 QQ (aiocqhttp)
        """
        # 1. 检查平台
        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("❌ 扫描功能仅支持 QQ 平台。")
            return

        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("❌ 请在群聊中使用此指令。")
            return

        # 2. 捕获当前会话的 UMO (Unified Message Origin)
        # 这是后续能成功发送消息的关键“通行证”
        current_umo = event.unified_msg_origin

        yield event.plain_result(f"⏳ 开始扫描群 {gid} 成员，请稍候...")
        
        count = 0
        try:
            # 调用底层 API 获取成员列表
            client = event.bot
            member_list = await client.api.call_action('get_group_member_list', group_id=int(gid))
            
            for member in member_list:
                uid = str(member['user_id'])
                # 优先获取群名片，没有则获取昵称
                nick = member.get('card') or member.get('nickname') or uid
                
                # 获取详细资料
                try:
                    info = await client.api.call_action('get_stranger_info', user_id=int(uid), no_cache=True)
                    if info and info.get("birthday_month") and info.get("birthday_day"):
                        m, d = info["birthday_month"], info["birthday_day"]
                        
                        # 存入数据库
                        self._add_record("birthdays", {
                            "user_id": uid,
                            "group_id": gid,
                            "date": f"{m:02d}-{d:02d}", # 格式化为 01-05
                            "name": nick,
                            "umo": current_umo  # 关键：存下 UMO
                        })
                        count += 1
                        logger.info(f"[Birthday] 扫描到: {nick} -> {m}-{d}")
                except Exception:
                    pass # 单个获取失败不影响整体
                
                # 延时防封控
                await asyncio.sleep(self.config.get("scan_interval", 3.0))
            
            yield event.plain_result(f"✅ 扫描完成！共更新 {count} 条生日数据。")

        except Exception as e:
            logger.error(f"[Birthday] Scan error: {e}")
            yield event.plain_result(f"❌ 扫描出错: {e}")

    @bd.command("add")
    async def add_birthday(self, event: AstrMessageEvent, date: str = None, user_id: str = None):
        """
        添加/更新生日 (仅限本群)
        用法:
        /bd add             -> 自动获取自己的生日
        /bd add 01-01       -> 手动设置自己的生日
        /bd add 01-01 QQ号  -> (管理员) 手动设置他人的生日
        """
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("❌ 请在群聊中使用此指令。")
            return

        tid = user_id if user_id else event.get_sender_id()
        tname = user_id if user_id else event.get_sender_name()
        
        # 捕获 UMO
        umo = event.unified_msg_origin

        # 模式1: 自动获取 (无日期参数)
        if not date:
            if event.get_platform_name() != "aiocqhttp":
                yield event.plain_result("⚠️ 非 QQ 平台不支持自动获取，请手动输入日期: /bd add 01-01")
                return
            
            try:
                info = await event.bot.api.call_action('get_stranger_info', user_id=int(tid), no_cache=True)
                if info and info.get("birthday_month"):
                    m, d = info["birthday_month"], info["birthday_day"]
                    ds = f"{m:02d}-{d:02d}"
                    real_name = info.get('nickname', tname)
                    
                    self._add_record("birthdays", {
                        "user_id": tid, "group_id": gid, "date": ds, 
                        "name": real_name, "umo": umo
                    })
                    yield event.plain_result(f"🎉 获取成功！已记录 {real_name} 的生日: {ds}")
                else:
                    yield event.plain_result("⚠️ 获取失败：你的资料未公开生日。请手动输入: /bd add MM-DD")
            except Exception as e:
                yield event.plain_result(f"❌ 获取出错: {e}")
            return

        # 模式2: 手动输入
        try:
            # 验证日期格式
            datetime.datetime.strptime(date, "%m-%d")
            
            self._add_record("birthdays", {
                "user_id": tid, "group_id": gid, "date": date, 
                "name": tname, "umo": umo
            })
            yield event.plain_result(f"✅ 已手动记录 {tname} 的生日: {date}")
        except ValueError:
            yield event.plain_result("❌ 日期格式错误，请使用 MM-DD (例如 01-25)")

    @bd.command("del")
    async def del_birthday(self, event: AstrMessageEvent):
        """删除自己在本当群的生日记录"""
        uid = event.get_sender_id()
        gid = event.get_group_id()
        if not gid: return
        
        orig_len = len(self.data["birthdays"])
        # 过滤掉匹配的记录
        self.data["birthdays"] = [
            x for x in self.data["birthdays"] 
            if not (x["user_id"] == uid and x["group_id"] == gid)
        ]
        
        if len(self.data["birthdays"]) < orig_len:
            self._save_data()
            yield event.plain_result("🗑️ 已删除你的生日记录。")
        else:
            yield event.plain_result("⚠️ 未找到你的记录。")

    @bd.command("add_ann")
    async def add_ann(self, event: AstrMessageEvent, date: str, name: str, desc: str = ""):
        """添加纪念日"""
        gid = event.get_group_id()
        if not gid:
             yield event.plain_result("❌ 请在群内使用。")
             return
        try:
            datetime.datetime.strptime(date, "%m-%d")
            # 同样需要存 UMO
            self.data["anniversaries"].append({
                "group_id": gid, "date": date, "name": name, 
                "desc": desc, "umo": event.unified_msg_origin
            })
            self._save_data()
            yield event.plain_result(f"✅ 已添加纪念日: {name}")
        except ValueError:
            yield event.plain_result("❌ 日期格式错误 (MM-DD)")

    @bd.command("list")
    async def list_all(self, event: AstrMessageEvent):
        """查看本群列表"""
        gid = event.get_group_id()
        if not gid: return
        
        # 检查白名单
        if gid not in self.config.get("group_whitelist", []):
            yield event.plain_result("⚠️ 本群未在 WebUI 白名单中，请联系管理员添加。")
            return

        msg = ["📅 本群清单:"]
        for bd in self.data["birthdays"]:
            if bd["group_id"] == gid:
                msg.append(f"[🎂] {bd['date']} {bd['name']}({bd['user_id']})")
        for ann in self.data["anniversaries"]:
            if ann["group_id"] == gid:
                msg.append(f"[🎉] {ann['date']} {ann['name']}")
        
        yield event.plain_result("\n".join(msg) if len(msg) > 1 else "本群暂无记录。")

    @bd.command("test")
    async def test_blessing(self, event: AstrMessageEvent):
        """测试发送 (立即触发一次模拟)"""
        gid = event.get_group_id()
        if not gid: return
        
        yield event.plain_result("🚀 正在触发测试发送...")
        
        # 构造临时数据
        test_data = [{
            "user_id": event.get_sender_id(),
            "name": event.get_sender_name(),
            "date": "01-01",
            "umo": event.unified_msg_origin # 使用当前的 UMO
        }]
        
        provider = self.context.get_using_provider()
        if not provider:
            yield event.plain_result("❌ 未配置 LLM 提供商。")
            return

        await self._send_batch_birthday(provider, test_data)

    # ================== 定时任务逻辑 ==================

    async def _scheduler_loop(self):
        """每分钟检查一次时间"""
        logger.info("[Birthday] 定时任务已启动。")
        try:
            while True:
                now = datetime.datetime.now()
                time_str = now.strftime("%H:%M")
                date_str = now.strftime("%m-%d")
                
                target_time = self.config.get("check_time", "08:00")
                
                # 只有当时间匹配，且今天还没发送过时才触发
                if time_str == target_time and self.last_check_date != date_str:
                    logger.info(f"[Birthday] 时间触发: {date_str} {time_str}")
                    await self._check_and_send(date_str)
                    self.last_check_date = date_str
                
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Birthday] 调度器错误: {e}")

    async def _check_and_send(self, today_str):
        """检查并发送祝福"""
        provider = self.context.get_using_provider()
        if not provider: return

        whitelist = self.config.get("group_whitelist", [])
        blacklist = self.config.get("user_blacklist", [])
        
        # 按 UMO 分组 (umo 代表了一个具体的会话上下文)
        batches = {} 
        
        for bd in self.data["birthdays"]:
            # 数据兼容性检查: 必须有 umo
            if "umo" not in bd: continue
            
            # 白名单/黑名单检查
            if (whitelist and bd["group_id"] not in whitelist) or (bd["user_id"] in blacklist): 
                continue
            
            if bd["date"] == today_str:
                batches.setdefault(bd["umo"], []).append(bd)
        
        # 发送生日祝福
        for umo, batch in batches.items():
            await self._send_batch_birthday(provider, batch)

        # 发送纪念日祝福
        for ann in self.data["anniversaries"]:
            if "umo" not in ann: continue
            if whitelist and ann["group_id"] not in whitelist: continue
            
            if ann["date"] == today_str:
                await self._send_anniversary(provider, ann)

    async def _send_batch_birthday(self, provider, user_list):
        if not user_list: return
        
        # 获取 UMO (所有用户在同一群，UMO相同)
        umo = user_list[0]["umo"]
        
        try:
            # 1. 获取人设 (使用框架原生方法)
            # 因为 umo 是合法的，这里一定能取到
            persona = await self.context.persona_manager.get_default_persona_v3(umo)
            sys_prompt = ""
            if persona:
                # 兼容性处理: 获取属性或字典值
                if hasattr(persona, "system_prompt"):
                    sys_prompt = persona.system_prompt
                elif isinstance(persona, dict):
                    sys_prompt = persona.get("system_prompt", "")

            # 2. 生成祝福语
            names = "、".join([u["name"] for u in user_list])
            tmpl = self.config.get("birthday_prompt", "")
            prompt = tmpl.replace("{date}", user_list[0]["date"]).replace("{name}", names)
            
            if len(user_list) > 1: 
                prompt += "\n(注: 今天有多位群友过生日，请写一段热闹的集体祝福)"

            resp = await provider.text_chat(prompt=prompt, system_prompt=sys_prompt, session_id=None)
            
            # 3. 构造消息链
            chain = []
            if self.config.get("at_target", True):
                for u in user_list: 
                    chain.extend([At(qq=u["user_id"]), Plain(" ")])
                chain.append(Plain("\n"))
            chain.append(Plain(resp.completion_text))
            
            # 4. 原生发送
            logger.info(f"[Birthday] Sending to {umo}")
            await self.context.send_message(umo, chain)
            
        except Exception as e:
            logger.error(f"[Birthday] 发送失败: {e}")

    async def _send_anniversary(self, provider, data):
        umo = data["umo"]
        try:
            base_tmpl = self.config.get("anniversary_prompt", "")
            desc = data.get("desc", "")
            prompt = f"{'描述:'+desc if desc else ''}\n{base_tmpl}".replace("{date}", data["date"]).replace("{event_name}", data["name"])
            
            # 获取人设
            persona = await self.context.persona_manager.get_default_persona_v3(umo)
            sys_prompt = ""
            if persona:
                if hasattr(persona, "system_prompt"):
                    sys_prompt = persona.system_prompt
                elif isinstance(persona, dict):
                    sys_prompt = persona.get("system_prompt", "")
            
            resp = await provider.text_chat(prompt=prompt, system_prompt=sys_prompt, session_id=None)
            
            # 原生发送
            await self.context.send_message(umo, [Plain(resp.completion_text)])
        except Exception as e:
            logger.error(f"[Birthday] 纪念日发送失败: {e}")
