"""Wecom (企业微信) AI Bot channel implementation using WebSocket."""

import asyncio
import json
import re
from typing import Any

import websockets
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import WecomConfig


class WecomChannel(BaseChannel):
    """
    企业微信 AI Bot channel using WebSocket long connection mode.

    Uses aibot-node-sdk compatible WebSocket protocol for:
    - Bot authentication
    - Message receive (text, voice, mixed)
    - Message send (markdown)
    - Heartbeat/keepalive
    """

    name = "wecom"

    def __init__(self, config: WecomConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: WecomConfig = config
        self._ws = None  # websockets 连接对象
        self._req_counter: int = 0  # req_id 自增计数器
        self._pending_req_ids: set[str] = set()  # 已发出待 ack 的 req_id
        self._missed_pongs: int = 0  # 连续未收到 pong 的次数
        self._background_tasks: set[asyncio.Task] = set()
        self._heartbeat_task: asyncio.Task | None = None

    def _next_req_id(self, prefix: str) -> str:
        """生成唯一的 req_id"""
        self._req_counter += 1
        return f"{prefix}_{self._req_counter}"

    async def start(self) -> None:
        """Start the Wecom bot with WebSocket connection."""
        if not self.config.bot_id or not self.config.secret:
            logger.error("Wecom bot_id and secret not configured")
            return

        self._running = True
        delay = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    self.config.ws_url,
                    ping_interval=None,  # 禁用库内置 ping，使用自定义 JSON 心跳
                    ping_timeout=None,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._missed_pongs = 0
                    delay = 1.0  # 连接成功后重置退避
                    if not await self._authenticate():
                        break  # 认证失败（配置错误），停止
                    self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                    self._background_tasks.add(self._heartbeat_task)
                    self._heartbeat_task.add_done_callback(self._background_tasks.discard)
                    await self._recv_loop(ws)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Wecom WS error: {}", e)
            finally:
                self._ws = None
                if self._heartbeat_task and not self._heartbeat_task.done():
                    self._heartbeat_task.cancel()
                    try:
                        await self._heartbeat_task
                    except asyncio.CancelledError:
                        pass
                self._heartbeat_task = None
            if self._running:
                logger.info("Reconnecting Wecom in {}s...", int(delay))
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

    async def _authenticate(self) -> bool:
        """发送认证帧并同步等待认证响应"""
        if not self._ws:
            return False

        req_id = self._next_req_id("aibot_subscribe")
        auth_frame = {
            "cmd": "aibot_subscribe",
            "headers": {"req_id": req_id},
            "body": {"bot_id": self.config.bot_id, "secret": self.config.secret},
        }
        await self._ws.send(json.dumps(auth_frame, ensure_ascii=False))
        logger.info("Wecom bot authentication sent, waiting for response...")

        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            data = json.loads(raw)
            # 认证响应格式：{ headers: { req_id: "..." }, errcode: 0 }
            resp_req_id = data.get("headers", {}).get("req_id")
            errcode = data.get("errcode")

            if resp_req_id == req_id and errcode == 0:
                logger.info("Wecom bot authenticated successfully")
                return True
            else:
                logger.error("Wecom authentication failed: errcode={}", errcode)
                return False
        except asyncio.TimeoutError:
            logger.error("Wecom authentication timed out")
            return False
        except Exception as e:
            logger.error("Wecom authentication error: {}", e)
            return False

    async def _recv_loop(self, ws) -> None:
        """消息接收主循环"""
        async for raw in ws:
            try:
                data = json.loads(raw)
                await self._handle_frame(data)
            except json.JSONDecodeError:
                logger.warning("Wecom received invalid JSON: {}", raw)
            except Exception as e:
                logger.error("Wecom error handling frame: {}", e)

    async def _heartbeat_loop(self) -> None:
        """心跳循环（含断线检测）"""
        while self._running and self._ws:
            try:
                await asyncio.sleep(self.config.heartbeat_interval)

                if not self._ws or self._ws.closed:
                    break

                # 检查是否连续丢失过多 pong
                if self._missed_pongs >= self.config.max_missed_pongs:
                    logger.warning(
                        "Wecom missed {} pongs, forcing reconnect",
                        self._missed_pongs,
                    )
                    await self._ws.close()
                    break

                self._missed_pongs += 1
                req_id = self._next_req_id("ping")
                ping_frame = {
                    "cmd": "ping",
                    "headers": {"req_id": req_id},
                }
                self._pending_req_ids.add(req_id)
                await self._ws.send(json.dumps(ping_frame, ensure_ascii=False))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Wecom heartbeat error: {}", e)
                break

    async def _handle_frame(self, data: dict) -> None:
        """帧分发处理"""
        errcode = data.get("errcode")
        headers = data.get("headers", {})
        req_id = headers.get("req_id", "")
        cmd = data.get("cmd")

        # Ack 帧：errcode is not None and no cmd
        if errcode is not None and not cmd:
            if req_id in self._pending_req_ids:
                self._pending_req_ids.discard(req_id)

            if req_id.startswith("ping_"):
                # pong 响应
                self._missed_pongs = 0
                logger.debug("Wecom received pong")
            elif req_id.startswith("aibot_send_msg_"):
                # 发送消息 ack
                if errcode != 0:
                    logger.warning("Wecom send msg failed: errcode={}", errcode)
                else:
                    logger.debug("Wecom send msg ack: success")
            return

        # 消息推送帧
        if cmd == "aibot_msg_callback":
            body = data.get("body", {})
            asyncio.create_task(self._on_message(body))
        elif cmd == "aibot_event_callback":
            logger.debug("Wecom event callback: {}", data)
        else:
            logger.debug("Wecom unknown frame: cmd={}", cmd)

    async def _on_message(self, body: dict) -> None:
        """解析并处理接收到的消息"""
        try:
            # 提取发送者
            from_info = body.get("from", {})
            sender_userid = from_info.get("userid", "")

            if not sender_userid:
                logger.warning("Wecom message missing from.userid")
                return

            # 确定 chat_id
            chattype = body.get("chattype", "")
            if chattype == "group":
                chatid = body.get("chatid", "")
                chat_id = f"group:{chatid}"
            else:
                chat_id = sender_userid

            # 提取消息内容
            content = ""
            msgtype = body.get("msgtype", "")

            if msgtype == "text":
                text_info = body.get("text", {})
                content = text_info.get("content", "")
            elif msgtype == "voice":
                voice_info = body.get("voice", {})
                content = voice_info.get("asr_content", "")
                if not content:
                    logger.debug("Wecom voice message has no asr_content, skipping")
                    return
            elif msgtype == "mixed":
                # 混合消息，拼接所有 text item 的 content
                mixed_info = body.get("mixed", {})
                items = mixed_info.get("item", [])
                text_contents = []
                for item in items:
                    if item.get("type") == "text":
                        text_contents.append(item.get("content", ""))
                content = "".join(text_contents)
            else:
                # image, file 等暂不支持
                logger.debug("Wecom unsupported msgtype: {}", msgtype)
                return

            # 群聊中去除 @xxx 提及标记
            if chattype == "group":
                content = re.sub(r"@\S+", "", content).strip()

            if not content:
                return

            logger.info(
                "Wecom message from {} ({}): {}",
                sender_userid,
                chat_id,
                content[:50],
            )

            await self._handle_message(
                sender_id=sender_userid,
                chat_id=chat_id,
                content=content,
                metadata={"raw": body},
            )
        except Exception as e:
            logger.error("Wecom error parsing message: {}", e)

    async def send(self, msg: OutboundMessage) -> None:
        """通过 WebSocket 发送消息"""
        if not self._ws or self._ws.closed:
            logger.warning("Wecom cannot send: not connected")
            return

        try:
            # 解析 chat_id
            if msg.chat_id.startswith("group:"):
                chatid = msg.chat_id[6:]
            else:
                chatid = msg.chat_id

            req_id = self._next_req_id("aibot_send_msg")
            frame = {
                "cmd": "aibot_send_msg",
                "headers": {"req_id": req_id},
                "body": {
                    "chatid": chatid,
                    "msgtype": "markdown",
                    "markdown": {"content": msg.content.strip()},
                },
            }
            self._pending_req_ids.add(req_id)
            await self._ws.send(json.dumps(frame, ensure_ascii=False))
            logger.debug("Wecom sent message to {}", chatid)
        except Exception as e:
            logger.error("Wecom error sending message: {}", e)

    async def stop(self) -> None:
        """优雅关闭"""
        self._running = False

        # 取消心跳任务
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # 关闭 WebSocket
        if self._ws and not self._ws.closed:
            await self._ws.close()

        # 取消所有后台任务
        for task in self._background_tasks:
            if not task.done():
                task.cancel()

        # 等待所有任务完成
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)

        logger.info("Wecom channel stopped")
