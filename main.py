import os
import json
import asyncio
import datetime
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.api.message_components import At, Plain

DATA_FILE = "birthday_data.json"

@register("astrbot_plugin_birthday", "Zhalslar_Assistant", "智能生日纪念日祝福", "1.5.0")
class BirthdayPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # 1. 数据持久化 (符合文档规范)
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_birthday")
        self.data_path = self.data_dir / DATA_FILE
        self.data = self._load_data()
        
        self.last_check_date = None
        self._task = asyncio.create_task(self._scheduler_loop())

    async def terminate(self):
        """插件卸载清理逻辑"""
        logger.info("[BirthdayPlugin] Terminating plugin...")
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[BirthdayPlugin] Terminated.")

    # ================== 数据管理 ==================
    def _load_data(self):
        if not self.data_path.exists():
            return {"birthdays": [], "anniversaries": []}
        try:
            with open(self.data_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 兼容旧数据
                for ann in data.get("anniversaries", []):
                    if "desc" not in ann: ann["desc"] = ""
                return data
        except Exception as e:
            logger.error(f"[BirthdayPlugin] Load data failed: {e}")
            return {"birthdays": [], "anniversaries": []}

    def _save_data(self):
        try:
            if not self.data_dir.exists():
                self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.data_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[BirthdayPlugin] Save data failed: {e}")

    def _add_birthday_record(self, user_id, group_id, date, name):
        self.data["birthdays"] = [
            x for x in self.data["birthdays"] 
            if not (x["user_id"] == user_id and x["group_id"] == group_id)
        ]
        self.data["birthdays"].append({
            "user_id": user_id,
            "group_id": group_id,
            "date": date,
            "name": name
        })
        self._save_data()

    # ================== 辅助函数 ==================
    
    async def _get_stranger_info(self, client, user_id):
        """
        标准化 API 调用 (参考文档 Page 1)
        使用 call_action 直接调用 OneBot API，兼容性更强
        """
        try:
            payload = {
                "user_id": int(user_id),
                "no_cache": True
            }
            # 文档推荐的标准调用方式
            return await client.api.call_action('get_stranger_info', **payload)
        except Exception as e:
            logger.warning(f"[Birthday] API call failed for {user_id}: {e}")
            return None

    # ================== 指令处理 ==================
    
    @filter.command_group("bd")
    def bd(self):
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bd.command("scan")
    async def scan_group(self, event: AstrMessageEvent, group_id: str = None):
        """(仅管理员) 扫描群成员资料"""
        if not isinstance(event, AiocqhttpMessageEvent):
            yield event.plain_result("❌ 仅支持 QQ (Aiocqhttp) 适配器。")
            return

        target_group = group_id if group_id else event.get_group_id()
        if not target_group:
            yield event.plain_result("❌ 未检测到群号。")
            return

        interval = self.config.get("scan_interval", 3.0)
        yield event.plain_result(f"⏳ 开始扫描群 {target_group}，间隔 {interval}s/人...")
        
        client = event.bot
        count = 0
        try:
            # 获取成员列表
            member_list = await client.api.call_action('get_group_member_list', group_id=int(target_group))
            
            for member in member_list:
                user_id = str(member['user_id'])
                nickname = member.get('card') or member.get('nickname') or user_id

                # 调用标准化 API 获取详情
                info = await self._get_stranger_info(client, user_id)
                if info:
                    m = info.get("birthday_month")
                    d = info.get("birthday_day")
                    if m and d:
                        date_str = f"{m:02d}-{d:02d}"
                        self._add_birthday_record(user_id, str(target_group), date_str, nickname)
                        count += 1
                        logger.info(f"[Birthday] Scanned: {nickname} -> {date_str}")

                await asyncio.sleep(interval)
            yield event.plain_result(f"✅ 扫描完成！获取到 {count} 条生日信息。")
        except Exception as e:
            yield event.plain_result(f"❌ 扫描出错: {e}")

    @bd.command("add")
    async def add_birthday(self, event: AstrMessageEvent, date: str = None, user_id: str = None, group_id: str = None):
        """添加生日"""
        target_id = user_id if user_id else event.get_sender_id()
        target_name = user_id if user_id else event.get_sender_name()
        target_group = group_id if group_id else event.get_group_id()

        if not target_group:
            yield event.plain_result("❌ 未检测到群号，请指定群号。")
            return

        # 自动拉取模式
        if not date:
            if not isinstance(event, AiocqhttpMessageEvent):
                yield event.plain_result("自动拉取仅支持 QQ。")
                return
            
            yield event.plain_result(f"🔍 正在获取 {target_id} 的资料...")
            client = event.bot
            info = await self._get_stranger_info(client, target_id)
            
            if info and info.get("birthday_month") and info.get("birthday_day"):
                m, d = info["birthday_month"], info["birthday_day"]
                date_str = f"{m:02d}-{d:02d}"
                fetched_name = info.get('nickname', target_name)
                
                self._add_birthday_record(target_id, target_group, date_str, fetched_name)
                yield event.plain_result(f"🎉 成功！已记录 {fetched_name} 的生日: {date_str}")
            else:
                yield event.plain_result("⚠️ 获取失败：资料未公开或为空。请手动输入: /bd add MM-DD")
            return

        # 手动模式
        try:
            datetime.datetime.strptime(date, "%m-%d")
            self._add_birthday_record(target_id, target_group, date, target_name)
            yield event.plain_result(f"✅ 已手动记录: {date}")
        except ValueError:
            yield event.plain_result("❌ 日期格式错误 (MM-DD)")

    @bd.command("del")
    async def del_birthday(self, event: AstrMessageEvent):
        """删除记录"""
        uid = event.get_sender_id()
        gid = event.get_group_id()
        if not gid: return
        
        orig_len = len(self.data["birthdays"])
        self.data["birthdays"] = [x for x in self.data["birthdays"] if not (x["user_id"] == uid and x["group_id"] == gid)]
        
        if len(self.data["birthdays"]) < orig_len:
            self._save_data()
            yield event.plain_result("🗑️ 已删除。")
        else:
            yield event.plain_result("⚠️ 未找到记录。")

    @bd.command("add_ann")
    async def add_ann(self, event: AstrMessageEvent, date: str, name: str, desc: str = ""):
        """添加纪念日"""
        try:
            datetime.datetime.strptime(date, "%m-%d")
            gid = event.get_group_id()
            if not gid:
                 yield event.plain_result("❌ 请在群聊中使用。")
                 return
            
            self.data["anniversaries"].append({
                "group_id": gid, "date": date, "name": name, "desc": desc
            })
            self._save_data()
            yield event.plain_result(f"✅ 已添加纪念日: {name}")
        except ValueError:
            yield event.plain_result("❌ 日期格式错误")

    @bd.command("list")
    async def list_all(self, event: AstrMessageEvent):
        """列表"""
        gid = event.get_group_id()
        if not gid: return
        
        if gid not in self.config.get("group_whitelist", []):
            yield event.plain_result("⚠️ 本群未在 WebUI 白名单中。")
            return

        msg = ["📅 清单:"]
        for bd in self.data["birthdays"]:
            if bd["group_id"] == gid:
                msg.append(f"[🎂] {bd['date']} {bd['name']}({bd['user_id']})")
        for ann in self.data["anniversaries"]:
            if ann["group_id"] == gid:
                msg.append(f"[🎉] {ann['date']} {ann['name']}")
        
        yield event.plain_result("\n".join(msg) if len(msg) > 1 else "暂无记录")

    @bd.command("test")
    async def test_blessing(self, event: AstrMessageEvent, type: str = "bd"):
        """测试指令"""
        gid = event.get_group_id()
        if not gid: return
        
        yield event.plain_result(f"🚀 测试 {type} 祝福...")
        provider = self.context.get_using_provider()
        if not provider:
            yield event.plain_result("❌ 无可用 LLM。")
            return

        if type == "ann":
            await self._send_anniversary(provider, {"group_id": gid, "date": "01-01", "name": "测试日", "desc": "测试用"})
        else:
            await self._send_batch_birthday(provider, gid, [{"user_id": event.get_sender_id(), "name": event.get_sender_name(), "date": "01-01"}])

    # ================== 定时任务 & 发送 ==================

    async def _scheduler_loop(self):
        logger.info("[BirthdayPlugin] Scheduler started.")
        try:
            while True:
                now = datetime.datetime.now()
                time_str = now.strftime("%H:%M")
                date_str = now.strftime("%m-%d")
                
                if time_str == self.config.get("check_time", "08:00") and self.last_check_date != date_str:
                    logger.info(f"[Birthday] Checking {date_str}")
                    await self._check_and_send(date_str)
                    self.last_check_date = date_str
                
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[Birthday] Scheduler error: {e}")

    async def _check_and_send(self, today_str):
        provider = self.context.get_using_provider()
        if not provider: return

        whitelist = self.config.get("group_whitelist", [])
        blacklist = self.config.get("user_blacklist", [])
        
        # 批量处理生日
        batches = {}
        for bd in self.data["birthdays"]:
            if (whitelist and bd["group_id"] not in whitelist) or (bd["user_id"] in blacklist): continue
            if bd["date"] == today_str:
                batches.setdefault(bd["group_id"], []).append(bd)
        
        for gid, batch in batches.items():
            await self._send_batch_birthday(provider, gid, batch)

        # 处理纪念日
        for ann in self.data["anniversaries"]:
            if whitelist and ann["group_id"] not in whitelist: continue
            if ann["date"] == today_str:
                await self._send_anniversary(provider, ann)

    async def _get_system_prompt(self, group_id):
        """
        [关键修复] 获取群组关联的人设 Prompt
        兼容性处理：无论返回 Personality 对象还是 dict 都能正常工作
        """
        try:
            # 构造 UMO (Doc Page 4)
            umo = f"aiocqhttp:group_message:{group_id}"
            
            # 获取人设 (Doc Page 7)
            persona = await self.context.persona_manager.get_default_persona_v3(umo)
            
            if not persona:
                return ""
            
            # 双重保险：既尝试作为属性访问，也尝试作为字典访问
            # 这是解决 "dict object has no attribute system_prompt" 的终极方案
            sp = getattr(persona, "system_prompt", None)
            if sp is None and isinstance(persona, dict):
                sp = persona.get("system_prompt", "")
            
            return sp or ""
        except Exception as e:
            logger.warning(f"[Birthday] Get persona failed: {e}")
            return ""

    async def _send_batch_birthday(self, provider, group_id, user_list):
        try:
            names = "、".join([u["name"] for u in user_list])
            tmpl = self.config.get("birthday_prompt", "")
            prompt = tmpl.replace("{date}", user_list[0]["date"]).replace("{name}", names)
            
            if len(user_list) > 1:
                prompt += f"\n(注：今天 {len(user_list)} 人过生日，请写一段热闹的集体祝福)"

            # 获取 System Prompt
            sys_prompt = await self._get_system_prompt(group_id)

            resp = await provider.text_chat(prompt=prompt, system_prompt=sys_prompt, session_id=None)
            
            chain = []
            if self.config.get("at_target", True):
                for u in user_list:
                    chain.extend([At(qq=u["user_id"]), Plain(" ")])
                chain.append(Plain("\n"))
            chain.append(Plain(resp.completion_text))
            
            await self._send_to_platform(group_id, chain)
        except Exception as e:
            logger.error(f"[Birthday] Send batch failed: {e}")

    async def _send_anniversary(self, provider, data):
        try:
            base_tmpl = self.config.get("anniversary_prompt", "")
            desc = data.get("desc", "")
            prompt = f"{'描述:'+desc if desc else ''}\n{base_tmpl}".replace("{date}", data["date"]).replace("{event_name}", data["name"])
            
            sys_prompt = await self._get_system_prompt(data["group_id"])
            
            resp = await provider.text_chat(prompt=prompt, system_prompt=sys_prompt, session_id=None)
            await self._send_to_platform(data["group_id"], [Plain(resp.completion_text)])
        except Exception as e:
            logger.error(f"[Birthday] Send ann failed: {e}")

    async def _send_to_platform(self, group_id, chain):
        platforms = self.context.platform_manager.get_insts()
        for p in platforms:
            if p.meta.type == "aiocqhttp":
                # 标准 UMO 格式 (Doc Page 4)
                target_umo = f"{p.meta.name}:group_message:{group_id}"
                try:
                    await self.context.send_message(target_umo, chain)
                    break
                except Exception as e:
                    logger.warning(f"[Birthday] Send error: {e}")
