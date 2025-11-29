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

@register("astrbot_plugin_birthday", "pigman02", "智能生日纪念日祝福", "1.3.2")
class BirthdayPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        
        # 使用规范的数据存储目录 data/plugin_data/astrbot_plugin_birthday/
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_birthday")
        self.data_path = self.data_dir / DATA_FILE
        
        self.data = self._load_data()
        self.last_check_date = None
        
        # 将任务句柄保存在实例变量中
        self._task = asyncio.create_task(self._scheduler_loop())

    async def terminate(self):
        """插件卸载/重载时的清理逻辑"""
        logger.info("[BirthdayPlugin] Terminating plugin...")
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.info("[BirthdayPlugin] Scheduler task cancelled successfully.")
            except Exception as e:
                logger.error(f"[BirthdayPlugin] Error during task cancellation: {e}")
        logger.info("[BirthdayPlugin] Plugin terminated.")

    # ================== 数据存储管理 ==================
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
        # 逻辑：先过滤掉该用户在该群的旧记录（实现覆盖更新）
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

    # ================== 指令处理区域 ==================
    
    @filter.command_group("bd")
    def bd(self):
        """生日助手指令组"""
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @bd.command("scan")
    async def scan_group(self, event: AstrMessageEvent, group_id: str = None):
        """(仅管理员) 扫描群成员资料自动登记生日"""
        if not isinstance(event, AiocqhttpMessageEvent):
            yield event.plain_result("❌ 此功能仅支持 QQ (Aiocqhttp) 适配器。")
            return

        target_group = group_id if group_id else event.get_group_id()

        if not target_group:
            yield event.plain_result("❌ 未检测到群号。私聊请指定群号: /bd scan [群号]")
            return

        interval = self.config.get("scan_interval", 3.0)
        yield event.plain_result(f"⏳ 开始扫描群 {target_group} 成员资料，间隔 {interval}秒/人，请耐心等待...")
        
        client = event.bot
        try:
            member_list = await client.get_group_member_list(group_id=int(target_group))
            count = 0
            
            for member in member_list:
                user_id = str(member['user_id'])
                nickname = member.get('card') or member.get('nickname') or user_id

                try:
                    info = await client.get_stranger_info(user_id=int(user_id), no_cache=True)
                    m = info.get("birthday_month")
                    d = info.get("birthday_day")
                    
                    if m and d:
                        date_str = f"{m:02d}-{d:02d}"
                        self._add_birthday_record(user_id, str(target_group), date_str, nickname)
                        count += 1
                        logger.info(f"[Birthday] Scanned: {nickname}({user_id}) -> {date_str} @ Group {target_group}")
                    
                except Exception:
                    pass

                await asyncio.sleep(interval)

            yield event.plain_result(f"✅ 扫描完成！共获取到 {count} 位成员的公开生日信息。")

        except Exception as e:
            logger.error(f"Scan failed: {e}")
            yield event.plain_result(f"❌ 扫描过程中发生错误: {e}")

    @bd.command("add")
    async def add_birthday(self, event: AstrMessageEvent, date: str = None, user_id: str = None, group_id: str = None):
        """添加生日 /bd add [date] [qq] [group]"""
        target_id = user_id if user_id else event.get_sender_id()
        if not user_id:
            target_name = event.get_sender_name()
        else:
            target_name = user_id 

        target_group = group_id if group_id else event.get_group_id()

        if not target_group:
            yield event.plain_result("❌ 未检测到群号。私聊请指定群号: /bd add 日期 QQ号 群号")
            return

        # --- 情况1: 自动拉取 ---
        if not date:
            if not isinstance(event, AiocqhttpMessageEvent):
                yield event.plain_result("自动拉取仅支持 QQ 适配器，请手动输入: /bd add MM-DD")
                return

            yield event.plain_result(f"🔍 正在获取 {target_id} 的公开资料...")
            try:
                client = event.bot
                info = await client.get_stranger_info(user_id=int(target_id), no_cache=True)
                m = info.get("birthday_month")
                d = info.get("birthday_day")
                
                fetched_name = info.get('nickname', target_name)

                if m and d:
                    date_str = f"{m:02d}-{d:02d}"
                    self._add_birthday_record(target_id, target_group, date_str, fetched_name)
                    yield event.plain_result(f"🎉 获取成功！已将 {fetched_name}({target_id}) 的生日 {date_str} 添加到群 {target_group}")
                else:
                    yield event.plain_result("⚠️ 获取失败：资料未设置生日或仅自己可见。\n请手动添加: /bd add MM-DD")
            except Exception as e:
                yield event.plain_result(f"❌ 获取资料出错: {e}")
            return

        # --- 情况2/3/4: 手动输入 ---
        try:
            datetime.datetime.strptime(date, "%m-%d")
            self._add_birthday_record(target_id, target_group, date, target_name)
            yield event.plain_result(f"✅ 已将 {target_id} 的生日 {date} 添加到群 {target_group}")
        except ValueError:
            yield event.plain_result("❌ 日期格式错误，请使用 MM-DD (例如 01-01)")

    @bd.command("del")
    async def del_birthday(self, event: AstrMessageEvent):
        """删除自己的生日记录"""
        user_id = event.get_sender_id()
        group_id = event.get_group_id()

        if not group_id:
            yield event.plain_result("请在群聊中使用此指令。")
            return

        # 记录原始长度
        original_len = len(self.data["birthdays"])
        
        # 过滤掉该用户在该群的记录
        self.data["birthdays"] = [
            x for x in self.data["birthdays"] 
            if not (x["user_id"] == user_id and x["group_id"] == group_id)
        ]
        
        # 检查长度是否变化
        if len(self.data["birthdays"]) < original_len:
            self._save_data()
            yield event.plain_result("🗑️ 已删除你在本群的生日记录。")
        else:
            yield event.plain_result("⚠️ 未找到你在本群的生日记录。")

    @bd.command("add_ann")
    async def add_ann(self, event: AstrMessageEvent, date: str, name: str, desc: str = ""):
        """添加纪念日 /bd add_ann date name [desc]"""
        try:
            datetime.datetime.strptime(date, "%m-%d")
            record = {
                "group_id": event.get_group_id(),
                "date": date,
                "name": name,
                "desc": desc
            }
            if not record["group_id"]:
                 yield event.plain_result("❌ 请在群聊中添加纪念日。")
                 return

            self.data["anniversaries"].append(record)
            self._save_data()
            
            resp = f"✅ 已添加纪念日: {name} ({date})"
            if desc:
                resp += f"\n自定义描述: {desc}"
            yield event.plain_result(resp)
        except ValueError:
            yield event.plain_result("❌ 日期格式错误，请使用 MM-DD")

    @bd.command("list")
    async def list_all(self, event: AstrMessageEvent):
        """查看本群记录清单"""
        gid = event.get_group_id()
        if not gid:
            yield event.plain_result("请在群聊中使用此指令查看该群列表。")
            return

        whitelist = self.config.get("group_whitelist", [])
        if gid not in whitelist:
            yield event.plain_result("⚠️ 本群未在配置白名单中，请联系管理员在 WebUI 添加，否则无法自动提醒。")
            return
            
        msg = ["📅 本群记录清单:"]
        
        group_bds = [x for x in self.data["birthdays"] if x["group_id"] == gid]
        for bd in group_bds:
            msg.append(f"[🎂] {bd['date']} - {bd['name']}({bd['user_id']})")
            
        group_anns = [x for x in self.data["anniversaries"] if x["group_id"] == gid]
        for ann in group_anns:
            hint = " (📝)" if ann.get("desc") else ""
            msg.append(f"[🎉] {ann['date']} - {ann['name']}{hint}")
            
        if len(msg) == 1:
            yield event.plain_result("本群暂无记录。")
        else:
            yield event.plain_result("\n".join(msg))

    # ================== 定时任务与发送逻辑 ==================

    async def _scheduler_loop(self):
        """后台循环检查时间"""
        logger.info("[BirthdayPlugin] Scheduler started.")
        try:
            while True:
                now = datetime.datetime.now()
                time_str = now.strftime("%H:%M")
                date_str = now.strftime("%m-%d")
                
                target_time = self.config.get("check_time", "08:00")
                
                if time_str == target_time and self.last_check_date != date_str:
                    logger.info(f"[BirthdayPlugin] Triggering check for {date_str}")
                    await self._check_and_send(date_str)
                    self.last_check_date = date_str
                
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info("[BirthdayPlugin] Scheduler loop stopping due to cancellation.")
            raise
        except Exception as e:
            logger.error(f"[BirthdayPlugin] Scheduler error: {e}")

    async def _check_and_send(self, today_str):
        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("[BirthdayPlugin] No LLM Provider available! Skipped.")
            return

        whitelist = self.config.get("group_whitelist", [])
        blacklist = self.config.get("user_blacklist", [])

        birthday_batches = {}

        for bd in self.data["birthdays"]:
            gid = bd["group_id"]
            uid = bd["user_id"]
            
            if whitelist and gid not in whitelist: continue
            if uid in blacklist: continue
            
            if bd["date"] == today_str:
                if gid not in birthday_batches:
                    birthday_batches[gid] = []
                birthday_batches[gid].append(bd)

        for gid, batch in birthday_batches.items():
            await self._send_batch_birthday(provider, gid, batch)

        for ann in self.data["anniversaries"]:
            if whitelist and ann["group_id"] not in whitelist: continue
            
            if ann["date"] == today_str:
                await self._send_anniversary(provider, ann)

    async def _send_batch_birthday(self, provider, group_id, user_list):
        try:
            names = [u["name"] for u in user_list]
            names_str = "、".join(names)
            date_str = user_list[0]["date"]
            
            tmpl = self.config.get("birthday_prompt", "")
            user_prompt = tmpl.replace("{date}", date_str).replace("{name}", names_str)
            
            if len(user_list) > 1:
                user_prompt += f"\n(注意：今天共有 {len(user_list)} 位群友同一天过生日，请在祝福中体现出“双喜临门”或“集体庆生”的热闹氛围。)"

            umo = f"aiocqhttp:group_message:{group_id}"
            persona = self.context.persona_manager.get_default_persona_v3(umo)
            system_prompt = persona.system_prompt if persona else ""

            logger.info(f"[Birthday] Generating batch wish for group {group_id}, users: {names_str}")

            resp = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                session_id=None
            )
            text = resp.completion_text

            chain = []
            should_at = self.config.get("at_target", True)
            
            if should_at:
                for u in user_list:
                    chain.append(At(qq=u["user_id"]))
                    chain.append(Plain(" "))
                chain.append(Plain("\n"))
            
            chain.append(Plain(text))

            await self._send_to_platform(group_id, chain)

        except Exception as e:
            logger.error(f"[Birthday] Batch send failed: {e}")

    async def _send_anniversary(self, provider, data):
        try:
            base_tmpl = self.config.get("anniversary_prompt", "")
            custom_desc = data.get("desc", "")
            
            context_desc = f"关于该纪念日的描述：{custom_desc}" if custom_desc else ""
            user_prompt = f"{context_desc}\n{base_tmpl}".replace("{date}", data["date"]).replace("{event_name}", data["name"])

            umo = f"aiocqhttp:group_message:{data['group_id']}"
            persona = self.context.persona_manager.get_default_persona_v3(umo)
            system_prompt = persona.system_prompt if persona else ""

            resp = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                session_id=None
            )
            
            chain = [Plain(resp.completion_text)]
            await self._send_to_platform(data["group_id"], chain)

        except Exception as e:
            logger.error(f"[Birthday] Anniversary send failed: {e}")

    async def _send_to_platform(self, group_id, chain):
        platforms = self.context.platform_manager.get_insts()
        for p in platforms:
            if p.meta.type == "aiocqhttp": 
                target_umo = f"{p.meta.name}:group_message:{group_id}"
                try:
                    await self.context.send_message(target_umo, chain)
                    break 
                except Exception as e:
                    logger.warning(f"[Birthday] Failed to send to {target_umo}: {e}")
