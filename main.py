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

@register("astrbot_plugin_birthday", "Zhalslar_Assistant", "智能生日纪念日祝福", "1.5.2")
class BirthdayPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # 数据持久化
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_birthday")
        self.data_path = self.data_dir / DATA_FILE
        self.data = self._load_data()
        
        self.last_check_date = None
        self._task = asyncio.create_task(self._scheduler_loop())

    async def terminate(self):
        """插件卸载清理逻辑"""
        logger.info("[BirthdayPlugin] Terminating...")
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
                data = json.load(f)
                for ann in data.get("anniversaries", []):
                    if "desc" not in ann: ann["desc"] = ""
                return data
        except Exception as e:
            logger.error(f"[Birthday] Load data failed: {e}")
            return {"birthdays": [], "anniversaries": []}

    def _save_data(self):
        try:
            if not self.data_dir.exists():
                self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.data_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            logger.error(f"[Birthday] Save data failed: {e}")

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

    # ================== 辅助函数 (API & Persona & Platform) ==================
    
    async def _get_stranger_info(self, client, user_id):
        """标准化调用 QQ API"""
        try:
            return await client.api.call_action('get_stranger_info', user_id=int(user_id), no_cache=True)
        except Exception as e:
            logger.warning(f"[Birthday] API Error: {e}")
            return None

    async def _get_system_prompt(self, group_id):
        """获取群组人设 (安全版)"""
        try:
            umo = f"aiocqhttp:group_message:{group_id}"
            persona = await self.context.persona_manager.get_default_persona_v3(umo)
            if not persona: return ""
            
            # 兼容对象和字典
            if isinstance(persona, dict):
                return persona.get("system_prompt", "")
            return getattr(persona, "system_prompt", "")
        except Exception as e:
            logger.warning(f"[Birthday] Get persona failed: {e}")
            return ""

    async def _send_to_platform(self, group_id, chain):
        """
        [关键修复] 发送消息辅助函数
        使用 get_platform 替代遍历，并处理 meta 为函数的情况
        """
        try:
            # 1. 直接获取 aiocqhttp 平台实例 (比遍历更安全)
            platform = self.context.get_platform("aiocqhttp")
            
            if not platform:
                logger.warning("[Birthday] AIOCQHTTP platform not found/active.")
                return

            # 2. 获取元数据 (防御性处理)
            meta = platform.meta
            if callable(meta): # 如果是方法，则调用它
                meta = meta()
            
            # 3. 获取平台名称
            # 优先取 name 属性，取不到则默认为 aiocqhttp
            p_name = getattr(meta, "name", "aiocqhttp")
            
            # 4. 构造 UMO 并发送
            target_umo = f"{p_name}:group_message:{group_id}"
            await self.context.send_message(target_umo, chain)
            
        except Exception as e:
            logger.warning(f"[Birthday] Send error: {e}")

    # ================== 指令处理 ==================
    
    @filter.command_group("bd")
    def bd(self):
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bd.command("scan")
    async def scan_group(self, event: AstrMessageEvent, group_id: str = None):
        """管理员扫描"""
        if not isinstance(event, AiocqhttpMessageEvent):
            yield event.plain_result("❌ 仅支持 QQ (Aiocqhttp)。")
            return

        target_group = group_id if group_id else event.get_group_id()
        if not target_group:
            yield event.plain_result("❌ 请指定群号。")
            return

        interval = self.config.get("scan_interval", 3.0)
        yield event.plain_result(f"⏳ 开始扫描群 {target_group}...")
        
        client = event.bot
        count = 0
        try:
            member_list = await client.api.call_action('get_group_member_list', group_id=int(target_group))
            for member in member_list:
                uid = str(member['user_id'])
                nick = member.get('card') or member.get('nickname') or uid
                
                info = await self._get_stranger_info(client, uid)
                if info and info.get("birthday_month") and info.get("birthday_day"):
                    m, d = info["birthday_month"], info["birthday_day"]
                    self._add_birthday_record(uid, str(target_group), f"{m:02d}-{d:02d}", nick)
                    count += 1
                
                await asyncio.sleep(interval)
            yield event.plain_result(f"✅ 扫描结束，更新 {count} 人。")
        except Exception as e:
            yield event.plain_result(f"❌ 错误: {e}")

    @bd.command("add")
    async def add_birthday(self, event: AstrMessageEvent, date: str = None, user_id: str = None, group_id: str = None):
        """添加生日"""
        tid = user_id if user_id else event.get_sender_id()
        tname = user_id if user_id else event.get_sender_name()
        tgid = group_id if group_id else event.get_group_id()

        if not tgid:
            yield event.plain_result("❌ 未知群号。")
            return

        # 自动获取
        if not date:
            if not isinstance(event, AiocqhttpMessageEvent):
                yield event.plain_result("仅支持QQ自动获取。")
                return
            
            info = await self._get_stranger_info(event.bot, tid)
            if info and info.get("birthday_month"):
                m, d = info["birthday_month"], info["birthday_day"]
                ds = f"{m:02d}-{d:02d}"
                real_name = info.get('nickname', tname)
                self._add_birthday_record(tid, tgid, ds, real_name)
                yield event.plain_result(f"🎉 已记录 {real_name} 生日: {ds}")
            else:
                yield event.plain_result("⚠️ 获取失败，请手动输入: /bd add 01-01")
            return

        # 手动输入
        try:
            datetime.datetime.strptime(date, "%m-%d")
            self._add_birthday_record(tid, tgid, date, tname)
            yield event.plain_result(f"✅ 已记录: {date}")
        except ValueError:
            yield event.plain_result("❌ 格式错误 (MM-DD)")

    @bd.command("del")
    async def del_birthday(self, event: AstrMessageEvent):
        """删除记录"""
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
        try:
            datetime.datetime.strptime(date, "%m-%d")
            gid = event.get_group_id()
            if not gid:
                 yield event.plain_result("❌ 请在群内使用。")
                 return
            self.data["anniversaries"].append({"group_id": gid, "date": date, "name": name, "desc": desc})
            self._save_data()
            yield event.plain_result(f"✅ 已添加: {name}")
        except ValueError:
            yield event.plain_result("❌ 日期错误")

    @bd.command("list")
    async def list_all(self, event: AstrMessageEvent):
        """查看列表"""
        gid = event.get_group_id()
        if not gid: return
        if gid not in self.config.get("group_whitelist", []):
            yield event.plain_result("⚠️ 本群未在白名单，请联系管理员配置。")
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
        """测试"""
        gid = event.get_group_id()
        if not gid: return
        
        yield event.plain_result(f"🚀 测试 {type} 祝福...")
        provider = self.context.get_using_provider()
        if not provider:
            yield event.plain_result("❌ 无可用 LLM。")
            return
        
        if type == "ann":
            await self._send_anniversary(provider, {"group_id": gid, "date": "01-01", "name": "测试日", "desc": "测试"})
        else:
            await self._send_batch_birthday(provider, gid, [{"user_id": event.get_sender_id(), "name": event.get_sender_name(), "date": "01-01"}])

    # ================== 定时与发送 ==================

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
        except Exception as e:
            logger.error(f"[Birthday] Loop error: {e}")

    async def _check_and_send(self, today_str):
        provider = self.context.get_using_provider()
        if not provider: return

        whitelist = self.config.get("group_whitelist", [])
        blacklist = self.config.get("user_blacklist", [])
        
        batches = {}
        for bd in self.data["birthdays"]:
            if (whitelist and bd["group_id"] not in whitelist) or (bd["user_id"] in blacklist): continue
            if bd["date"] == today_str:
                batches.setdefault(bd["group_id"], []).append(bd)
        
        for gid, batch in batches.items():
            await self._send_batch_birthday(provider, gid, batch)

        for ann in self.data["anniversaries"]:
            if whitelist and ann["group_id"] not in whitelist: continue
            if ann["date"] == today_str:
                await self._send_anniversary(provider, ann)

    async def _send_batch_birthday(self, provider, group_id, user_list):
        try:
            names = "、".join([u["name"] for u in user_list])
            tmpl = self.config.get("birthday_prompt", "")
            prompt = tmpl.replace("{date}", user_list[0]["date"]).replace("{name}", names)
            if len(user_list) > 1: prompt += "\n(注: 多人同一天生日)"

            sys_prompt = await self._get_system_prompt(group_id)
            
            resp = await provider.text_chat(prompt=prompt, system_prompt=sys_prompt, session_id=None)
            
            chain = []
            if self.config.get("at_target", True):
                for u in user_list: chain.extend([At(qq=u["user_id"]), Plain(" ")])
                chain.append(Plain("\n"))
            chain.append(Plain(resp.completion_text))
            
            await self._send_to_platform(group_id, chain)
        except Exception as e:
            logger.error(f"[Birthday] Batch error: {e}")

    async def _send_anniversary(self, provider, data):
        try:
            base_tmpl = self.config.get("anniversary_prompt", "")
            desc = data.get("desc", "")
            prompt = f"{'描述:'+desc if desc else ''}\n{base_tmpl}".replace("{date}", data["date"]).replace("{event_name}", data["name"])
            
            sys_prompt = await self._get_system_prompt(data["group_id"])
            
            resp = await provider.text_chat(prompt=prompt, system_prompt=sys_prompt, session_id=None)
            await self._send_to_platform(data["group_id"], [Plain(resp.completion_text)])
        except Exception as e:
            logger.error(f"[Birthday] Ann error: {e}")
