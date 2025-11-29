import os
import json
import asyncio
import datetime
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.api.message_components import At, Plain

DATA_FILE = "birthday_data.json"

@register("astrbot_plugin_birthday", "Zhalslar_Assistant", "智能生日纪念日祝福", "2.0.0")
class BirthdayPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_birthday")
        self.data_path = self.data_dir / DATA_FILE
        self.data = self._load_data()
        
        self.last_check_date = None
        self._task = asyncio.create_task(self._scheduler_loop())

    async def terminate(self):
        logger.info("[Birthday] Terminating...")
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ================== 数据管理 ==================
    def _load_data(self):
        if not self.data_path.exists():
            return {"birthdays": [], "anniversaries": []}
        try:
            with open(self.data_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[Birthday] Load failed: {e}")
            return {"birthdays": [], "anniversaries": []}

    def _save_data(self):
        try:
            if not self.data_dir.exists():
                self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.data_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[Birthday] Save failed: {e}")

    def _add_record(self, record_type, data):
        """通用添加记录方法"""
        # 移除旧记录 (同群同人/同名)
        target_list = self.data[record_type]
        if record_type == "birthdays":
            self.data[record_type] = [
                x for x in target_list 
                if not (x["user_id"] == data["user_id"] and x["group_id"] == data["group_id"])
            ]
        
        self.data[record_type].append(data)
        self._save_data()

    # ================== 核心逻辑 (原生) ==================

    async def _get_stranger_info(self, event, user_id):
        """使用 event.bot (Adapter) 获取信息"""
        try:
            # 既然只能在群里用，event.bot 一定是存在的
            return await event.bot.api.call_action('get_stranger_info', user_id=int(user_id), no_cache=True)
        except Exception:
            return None

    # ================== LLM Tools ==================

    @filter.llm_tool(name="record_birthday")
    async def record_birthday_tool(self, event: AstrMessageEvent, name: str, date: str):
        """
        记录自己的生日。
        Args:
            name: 名字
            date: 日期 MM-DD
        """
        if not event.get_group_id():
            yield event.plain_result("请在群聊使用。")
            return
        
        try:
            datetime.datetime.strptime(date, "%m-%d")
        except ValueError:
            yield event.plain_result("日期格式错误。")
            return

        # 直接捕获当前的 UMO，这是解决问题的关键！
        umo = event.unified_msg_origin
        
        record = {
            "user_id": event.get_sender_id(),
            "group_id": event.get_group_id(),
            "date": date,
            "name": name,
            "umo": umo  # <--- 核心：存下这个“通行证”
        }
        
        self._add_record("birthdays", record)
        yield event.plain_result(f"✅ 已记录 {name} 的生日 {date}。")

    # ================== 指令处理 ==================
    
    @filter.command_group("bd")
    def bd(self):
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bd.command("scan")
    async def scan_group(self, event: AstrMessageEvent):
        """扫描当前群"""
        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("❌ 仅支持 QQ。")
            return

        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("❌ 请在群聊使用。")
            return

        # 捕获 UMO
        current_umo = event.unified_msg_origin

        yield event.plain_result(f"⏳ 扫描中...")
        
        count = 0
        try:
            member_list = await event.bot.api.call_action('get_group_member_list', group_id=int(gid))
            for member in member_list:
                uid = str(member['user_id'])
                nick = member.get('card') or member.get('nickname') or uid
                
                info = await event.bot.api.call_action('get_stranger_info', user_id=int(uid), no_cache=True)
                if info and info.get("birthday_month") and info.get("birthday_day"):
                    m, d = info["birthday_month"], info["birthday_day"]
                    
                    self._add_record("birthdays", {
                        "user_id": uid,
                        "group_id": gid,
                        "date": f"{m:02d}-{d:02d}",
                        "name": nick,
                        "umo": current_umo # 所有人都共用这个群的 UMO
                    })
                    count += 1
                
                await asyncio.sleep(self.config.get("scan_interval", 3.0))
            
            yield event.plain_result(f"✅ 扫描结束，更新 {count} 人。")
        except Exception as e:
            logger.error(f"Scan error: {e}")
            yield event.plain_result("❌ 扫描出错，请查看日志。")

    @bd.command("add")
    async def add_birthday(self, event: AstrMessageEvent, date: str = None, user_id: str = None):
        """添加/更新生日 (仅限本群)"""
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("❌ 请在群聊中使用。")
            return

        tid = user_id if user_id else event.get_sender_id()
        tname = user_id if user_id else event.get_sender_name()
        
        # 核心：捕获当前会话的 UMO
        umo = event.unified_msg_origin

        # 自动获取逻辑
        if not date:
            if event.get_platform_name() != "aiocqhttp":
                yield event.plain_result("非 QQ 平台请手动输入日期。")
                return
            
            info = await self._get_stranger_info(event, tid)
            if info and info.get("birthday_month"):
                m, d = info["birthday_month"], info["birthday_day"]
                ds = f"{m:02d}-{d:02d}"
                real_name = info.get('nickname', tname)
                
                self._add_record("birthdays", {
                    "user_id": tid, "group_id": gid, "date": ds, 
                    "name": real_name, "umo": umo
                })
                yield event.plain_result(f"🎉 已获取 {real_name} 生日: {ds}")
            else:
                yield event.plain_result("⚠️ 获取失败，请手动输入: /bd add 01-01")
            return

        # 手动输入逻辑
        try:
            datetime.datetime.strptime(date, "%m-%d")
            self._add_record("birthdays", {
                "user_id": tid, "group_id": gid, "date": date, 
                "name": tname, "umo": umo
            })
            yield event.plain_result(f"✅ 已记录: {date}")
        except ValueError:
            yield event.plain_result("❌ 格式错误 (MM-DD)")

    @bd.command("del")
    async def del_birthday(self, event: AstrMessageEvent):
        """删除本群记录"""
        uid = event.get_sender_id()
        gid = event.get_group_id()
        if not gid: return
        
        orig = len(self.data["birthdays"])
        self.data["birthdays"] = [x for x in self.data["birthdays"] if not (x["user_id"] == uid and x["group_id"] == gid)]
        
        if len(self.data["birthdays"]) < orig:
            self._save_data()
            yield event.plain_result("🗑️ 已删除。")
        else:
            yield event.plain_result("⚠️ 无记录。")

    @bd.command("add_ann")
    async def add_ann(self, event: AstrMessageEvent, date: str, name: str, desc: str = ""):
        """添加纪念日"""
        gid = event.get_group_id()
        if not gid:
             yield event.plain_result("❌ 请在群内使用。")
             return
        try:
            datetime.datetime.strptime(date, "%m-%d")
            self.data["anniversaries"].append({
                "group_id": gid, "date": date, "name": name, 
                "desc": desc, "umo": event.unified_msg_origin
            })
            self._save_data()
            yield event.plain_result(f"✅ 已添加: {name}")
        except ValueError:
            yield event.plain_result("❌ 日期错误")

    @bd.command("list")
    async def list_all(self, event: AstrMessageEvent):
        gid = event.get_group_id()
        if not gid: return
        
        msg = ["📅 本群清单:"]
        for bd in self.data["birthdays"]:
            if bd["group_id"] == gid:
                msg.append(f"[🎂] {bd['date']} {bd['name']}({bd['user_id']})")
        for ann in self.data["anniversaries"]:
            if ann["group_id"] == gid:
                msg.append(f"[🎉] {ann['date']} {ann['name']}")
        yield event.plain_result("\n".join(msg) if len(msg) > 1 else "暂无记录")

    @bd.command("test")
    async def test_blessing(self, event: AstrMessageEvent):
        """测试发送（使用捕获的 UMO）"""
        gid = event.get_group_id()
        if not gid: return
        
        # 构造一个临时数据包进行测试
        test_data = [{
            "user_id": event.get_sender_id(),
            "name": event.get_sender_name(),
            "date": "01-01",
            "umo": event.unified_msg_origin # 使用当前的 UMO
        }]
        
        yield event.plain_result("🚀 测试触发...")
        provider = self.context.get_using_provider()
        await self._send_batch_birthday(provider, test_data)

    # ================== 定时任务 & 原生发送 ==================

    async def _scheduler_loop(self):
        logger.info("[Birthday] Task started.")
        try:
            while True:
                now = datetime.datetime.now()
                if now.strftime("%H:%M") == self.config.get("check_time", "08:00") and self.last_check_date != now.strftime("%m-%d"):
                    await self._check_and_send(now.strftime("%m-%d"))
                    self.last_check_date = now.strftime("%m-%d")
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass

    async def _check_and_send(self, today_str):
        provider = self.context.get_using_provider()
        if not provider: return

        whitelist = self.config.get("group_whitelist", [])
        blacklist = self.config.get("user_blacklist", [])
        
        # 按 UMO (本质上是按群) 分组
        batches = {} # { umo: [record1, record2] }
        
        for bd in self.data["birthdays"]:
            # 兼容性检查：如果旧数据没有 umo，跳过（建议用户重新添加）
            if "umo" not in bd: continue
            if (whitelist and bd["group_id"] not in whitelist) or (bd["user_id"] in blacklist): continue
            
            if bd["date"] == today_str:
                batches.setdefault(bd["umo"], []).append(bd)
        
        # 发送生日
        for umo, batch in batches.items():
            await self._send_batch_birthday(provider, batch)

        # 发送纪念日
        for ann in self.data["anniversaries"]:
            if "umo" not in ann: continue
            if whitelist and ann["group_id"] not in whitelist: continue
            if ann["date"] == today_str:
                await self._send_anniversary(provider, ann)

    async def _send_batch_birthday(self, provider, user_list):
        if not user_list: return
        
        # 从记录中取出 UMO，这一定是合法的，因为是 AstrBot 自己生成的
        umo = user_list[0]["umo"]
        
        try:
            # 1. 获取人设：现在我们有合法的 UMO 了，获取人设绝对没问题
            persona = await self.context.persona_manager.get_default_persona_v3(umo)
            sys_prompt = ""
            if persona:
                sys_prompt = getattr(persona, "system_prompt", "") or persona.get("system_prompt", "")

            # 2. 生成祝福
            names = "、".join([u["name"] for u in user_list])
            tmpl = self.config.get("birthday_prompt", "")
            prompt = tmpl.replace("{date}", user_list[0]["date"]).replace("{name}", names)
            if len(user_list) > 1: prompt += "\n(注: 多人同一天生日)"

            resp = await provider.text_chat(prompt=prompt, system_prompt=sys_prompt, session_id=None)
            
            # 3. 构造原生消息链
            chain = []
            if self.config.get("at_target", True):
                for u in user_list: chain.extend([At(qq=u["user_id"]), Plain(" ")])
                chain.append(Plain("\n"))
            chain.append(Plain(resp.completion_text))
            
            # 4. 原生发送！！
            logger.info(f"[Birthday] Native sending to {umo}")
            await self.context.send_message(umo, chain)
            
        except Exception as e:
            logger.error(f"[Birthday] Send failed: {e}")

    async def _send_anniversary(self, provider, data):
        umo = data["umo"]
        try:
            base_tmpl = self.config.get("anniversary_prompt", "")
            desc = data.get("desc", "")
            prompt = f"{'描述:'+desc if desc else ''}\n{base_tmpl}".replace("{date}", data["date"]).replace("{event_name}", data["name"])
            
            # 原生获取人设
            persona = await self.context.persona_manager.get_default_persona_v3(umo)
            sys_prompt = ""
            if persona:
                sys_prompt = getattr(persona, "system_prompt", "") or persona.get("system_prompt", "")
            
            resp = await provider.text_chat(prompt=prompt, system_prompt=sys_prompt, session_id=None)
            
            # 原生发送
            await self.context.send_message(umo, [Plain(resp.completion_text)])
        except Exception as e:
            logger.error(f"[Birthday] Ann failed: {e}")
