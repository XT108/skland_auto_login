# notifier.py
import base64
import hashlib
import hmac
import httpx
import logging
import os
import smtplib
import time
import urllib.parse
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

logger = logging.getLogger("notifier")


def _apply_env_notify_config(notify_cfg: dict) -> None:
    """把环境变量中的通知配置合并进 notify 节点（GitHub Actions 场景用）。

    config.yaml 里显式填写的值优先，环境变量只做兜底，不会覆盖已有配置。
    """
    env_map = {
        "dingtalk": {
            "webhook_url": os.getenv("DINGTALK_WEBHOOK", "").strip(),
            "secret": os.getenv("DINGTALK_SECRET", "").strip(),
            "keyword": os.getenv("DINGTALK_KEYWORD", "").strip(),
            "mode": os.getenv("DINGTALK_MODE", "").strip(),
            "repo": os.getenv("DINGTALK_REPO", "").strip(),
        },
        "qmsg": {
            "key": os.getenv("QMSG_KEY", "").strip(),
        },
        "onebot": {
            "url": os.getenv("ONEBOT_URL", "").strip(),
            "access_token": os.getenv("ONEBOT_ACCESS_TOKEN", "").strip(),
        },
        "wecom": {
            "webhook_url": os.getenv("WECOM_WEBHOOK", "").strip(),
        },
        "serverchan": {
            "send_key": os.getenv("SERVERCHAN_SEND_KEY", "").strip(),
        },
        "email": {
            "smtp_host": os.getenv("EMAIL_SMTP_HOST", "").strip(),
            "smtp_port": os.getenv("EMAIL_SMTP_PORT", "").strip(),
            "username": os.getenv("EMAIL_USERNAME", "").strip(),
            "password": os.getenv("EMAIL_PASSWORD", "").strip(),
            "receiver": os.getenv("EMAIL_RECEIVER", "").strip(),
        },
    }

    for section, pairs in env_map.items():
        existing = notify_cfg.get(section)
        if not isinstance(existing, dict):
            existing = {}
            notify_cfg[section] = existing
        for key, value in pairs.items():
            if value and not existing.get(key):
                existing[key] = value


class NotifierManager:
    """统一通知管理器，根据配置自动选择可用的推送渠道"""

    def __init__(self, config: dict):
        self.notifiers = []
        notify_cfg = config.get("notify", {})
        if not isinstance(notify_cfg, dict):
            notify_cfg = {}

        # 环境变量兜底（GitHub Actions 没有 config.yaml 时全靠这里）
        _apply_env_notify_config(notify_cfg)

        # ---------- 钉钉群机器人 ----------
        dingtalk_cfg = notify_cfg.get("dingtalk") or {}
        if dingtalk_cfg.get("webhook_url"):
            self.notifiers.append(DingTalkNotifier(dingtalk_cfg))

        # 兼容老版本的 qmsg_key 配置
        legacy_qmsg_key = config.get("qmsg_key")
        qmsg_key = notify_cfg.get("qmsg", {}).get("key") or legacy_qmsg_key

        if qmsg_key:
            qmsg_cfg = notify_cfg.get("qmsg", {})
            qmsg_cfg["key"] = qmsg_key
            self.notifiers.append(QmsgNotifier(qmsg_cfg))

        if notify_cfg.get("onebot", {}).get("url"):
            self.notifiers.append(OneBotNotifier(notify_cfg["onebot"]))

        if notify_cfg.get("email", {}).get("smtp_host"):
            email_cfg = notify_cfg["email"]
            # 强制将密码转为字符串，防止纯数字密码报错
            email_cfg["password"] = str(email_cfg.get("password", ""))
            self.notifiers.append(EmailNotifier(email_cfg))

        if notify_cfg.get("wecom", {}).get("webhook_url"):
            self.notifiers.append(WeComNotifier(notify_cfg["wecom"]))

        if notify_cfg.get("wechat_mp", {}).get("app_id"):
            self.notifiers.append(WeChatMPNotifier(notify_cfg["wechat_mp"]))

        if notify_cfg.get("serverchan", {}).get("send_key"):
            self.notifiers.append(ServerChanNotifier(notify_cfg["serverchan"]))

        if not self.notifiers:
            logger.info("未配置任何通知渠道，跳过推送")

    async def send_all(self, message: str):
        """向所有已启用的渠道发送通知"""
        if not self.notifiers:
            return

        for notifier in self.notifiers:
            try:
                await notifier.send(message)
            except Exception as e:
                logger.error(f"[{notifier.name}] 推送异常: {e}")


class BaseNotifier:
    """通知基类"""
    name = "base"

    async def send(self, message: str) -> bool:
        raise NotImplementedError


# ==================== 钉钉群机器人 ====================
class DingTalkNotifier(BaseNotifier):
    """钉钉群机器人推送。

    支持两类群机器人（群设置 -> 智能群助手 里添加的机器人不同，用的接口格式也不同）：

    mode="custom"（默认，推荐）
        群内「自定义机器人」，走标准 text 消息，内容完全可控。
        安全设置支持：自定义关键词（keyword）/ 加签（secret）/ IP 白名单（Actions 不适用）。

    mode="github"
        群内「GitHub 机器人」。它只认 GitHub 的 webhook 事件格式，普通 text 消息会返回
        errcode 300001 "robot type do not match with the message"。
        此模式会伪造一条 push 事件，需附带 X-GitHub-Event 请求头。
    """

    name = "钉钉"

    def __init__(self, cfg: dict):
        self.webhook_url = (cfg.get("webhook_url") or "").strip()
        self.mode = (cfg.get("mode") or "custom").strip().lower()
        self.secret = (cfg.get("secret") or "").strip()        # 加签密钥（SEC 开头），可选
        self.keyword = (cfg.get("keyword") or "").strip()      # 自定义关键词，可选
        self.at_mobiles = cfg.get("at_mobiles") or []
        self.repo = (cfg.get("repo") or "XT108/skland_auto_login").strip()
        self.sender_name = (cfg.get("sender_name") or "森空岛签到姬").strip()

        if self.mode not in ("custom", "github"):
            logger.warning(f"[钉钉] 未知 mode={self.mode!r}，已回退为 custom")
            self.mode = "custom"

    # ---------- 加签 ----------
    def _signed_url(self) -> str:
        """按钉钉规则对 URL 加签（仅在配置了 secret 时生效）。"""
        if not self.secret:
            return self.webhook_url
        ts = str(round(time.time() * 1000))
        string_to_sign = f"{ts}\n{self.secret}"
        digest = hmac.new(self.secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest))
        sep = "&" if "?" in self.webhook_url else "?"
        return f"{self.webhook_url}{sep}timestamp={ts}&sign={sign}"

    # ---------- 报文构造 ----------
    def _build_payload(self, message: str, mode: str | None = None) -> tuple[dict, dict]:
        """返回 (payload, extra_headers)。mode 为 None 时使用实例配置。"""
        mode = (mode or self.mode).lower()
        if mode == "github":
            commit_id = hashlib.sha1(message.encode("utf-8")).hexdigest()
            html_url = f"https://github.com/{self.repo}"
            commit = {
                "id": commit_id,
                "message": message,
                "url": html_url,
                "author": {"name": self.sender_name, "email": "bot@users.noreply.github.com"},
                "added": [],
                "removed": [],
                "modified": [],
            }
            payload = {
                "ref": "refs/heads/main",
                "before": "0" * 40,
                "after": commit_id,
                "repository": {
                    "id": 0,
                    "name": self.repo.split("/")[-1],
                    "full_name": self.repo,
                    "html_url": html_url,
                    "description": "森空岛自动签到",
                    "default_branch": "main",
                },
                "pusher": {"name": self.sender_name, "email": "bot@users.noreply.github.com"},
                "sender": {"login": self.sender_name, "id": 0},
                "commits": [commit],
                "head_commit": commit,
            }
            headers = {
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": commit_id[:32],
                "X-GitHub-Hook-ID": "1",
            }
            return payload, headers

        content = message
        if self.keyword and self.keyword not in content:
            # 机器人若启用了「自定义关键词」校验，正文必须包含该关键词
            content = f"{self.keyword}\n{content}"

        payload = {"msgtype": "text", "text": {"content": content}}
        if self.at_mobiles:
            payload["at"] = {"atMobiles": list(self.at_mobiles), "isAtAll": False}
        return payload, {}

    async def send(self, message: str) -> bool:
        ok, result = await self._post(message, self.mode)

        # 自愈：机器人类型与消息格式不匹配时，换另一种格式重试一次
        # （custom <-> github 互切；失败的请求不会产生群消息，重试是安全的）
        if not ok and result.get("errcode") == 300001:
            other = "github" if self.mode != "github" else "custom"
            logger.warning(
                f"[钉钉] 当前 mode={self.mode} 与机器人类型不匹配（300001），"
                f"自动改用 mode={other} 重试一次"
            )
            ok, result = await self._post(message, other)
            if ok:
                logger.warning(
                    f"[钉钉] 重试成功。建议把配置里的 mode 固定为 {other}，省掉这次多余的握手。"
                )

        if ok:
            logger.info(f"[钉钉] 推送成功（mode={self.mode}）")
            return True

        errcode = result.get("errcode")
        logger.error(f"[钉钉] 推送失败: errcode={errcode} errmsg={result.get('errmsg')}")
        if errcode == 300001:
            logger.error(
                "[钉钉] 300001 说明机器人类型与消息格式不匹配："
                "若你用普通 text 消息却被拒，说明该 webhook 属于「GitHub 机器人」，"
                "请把 mode 设为 github，或改用群内「自定义机器人」的 webhook。"
            )
        elif errcode == 310000:
            logger.error("[钉钉] 310000 多为安全设置不通过：关键词未命中、加签密钥缺失或错误。")
        elif errcode in (300005, 300006):
            logger.error("[钉钉] 请确认 webhook 的 access_token 是否正确、机器人是否已被移出群。")
        return False

    async def _post(self, message: str, mode: str) -> tuple[bool, dict]:
        """按指定 mode 发一次请求，返回 (是否成功, 响应体)。"""
        payload, extra_headers = self._build_payload(message, mode)
        headers = {"Content-Type": "application/json;charset=utf-8"}
        headers.update(extra_headers)

        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.post(self._signed_url(), json=payload, headers=headers)
                result = resp.json()
        except Exception as e:
            logger.error(f"[钉钉] 推送异常: {e}")
            return False, {"errcode": -1, "errmsg": str(e)}

        return result.get("errcode") == 0, result


# ==================== Qmsg 酱 ====================
class QmsgNotifier(BaseNotifier):
    name = "Qmsg"

    def __init__(self, cfg: dict):
        self.key = cfg["key"]
        self.base_url = cfg.get("base_url", "https://qmsg.zendee.cn")

    async def send(self, message: str) -> bool:
        url = f"{self.base_url}/send/{self.key}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data={"msg": message})
            result = resp.json()
            if result.get("success"):
                logger.info("[Qmsg] 推送成功")
                return True
            else:
                logger.error(f"[Qmsg] 推送失败: {result.get('reason')}")
                return False


# ==================== OneBot V11 (NapCat等) ====================
class OneBotNotifier(BaseNotifier):
    name = "OneBot"

    def __init__(self, cfg: dict):
        self.url = cfg["url"].rstrip("/")
        self.access_token = cfg.get("access_token", "")
        # 支持多个私聊目标
        self.private_ids = self._parse_ids(cfg.get("private_ids", []))
        # 支持多个群聊目标
        self.group_ids = self._parse_ids(cfg.get("group_ids", []))

    @staticmethod
    def _parse_ids(raw) -> list[int]:
        """将配置值统一解析为 int 列表，支持单个值或列表"""
        if not raw:
            return []
        if isinstance(raw, (int, str)):
            raw = [raw]
        return [int(i) for i in raw if str(i).strip()]

    async def send(self, message: str) -> bool:
        if not self.private_ids and not self.group_ids:
            logger.error("[OneBot] 未配置 private_ids 或 group_ids")
            return False

        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        all_success = True

        async with httpx.AsyncClient() as client:
            # 发送私聊
            for user_id in self.private_ids:
                try:
                    resp = await client.post(
                        f"{self.url}/send_private_msg",
                        json={"user_id": user_id, "message": message},
                        headers=headers,
                    )
                    result = resp.json()
                    if result.get("status") == "ok" or result.get("retcode") == 0:
                        logger.info(f"[OneBot] 私聊推送成功 -> {user_id}")
                    else:
                        logger.error(f"[OneBot] 私聊推送失败 -> {user_id}: {result}")
                        all_success = False
                except Exception as e:
                    logger.error(f"[OneBot] 私聊推送异常 -> {user_id}: {e}")
                    all_success = False

            # 发送群聊
            for group_id in self.group_ids:
                try:
                    resp = await client.post(
                        f"{self.url}/send_group_msg",
                        json={"group_id": group_id, "message": message},
                        headers=headers,
                    )
                    result = resp.json()
                    if result.get("status") == "ok" or result.get("retcode") == 0:
                        logger.info(f"[OneBot] 群聊推送成功 -> {group_id}")
                    else:
                        logger.error(f"[OneBot] 群聊推送失败 -> {group_id}: {result}")
                        all_success = False
                except Exception as e:
                    logger.error(f"[OneBot] 群聊推送异常 -> {group_id}: {e}")
                    all_success = False

        return all_success

# ==================== 邮件 ====================
class EmailNotifier(BaseNotifier):
    name = "Email"

    def __init__(self, cfg: dict):
        self.smtp_host = cfg["smtp_host"]
        self.smtp_port = cfg.get("smtp_port", 465)
        self.use_ssl = cfg.get("use_ssl", True)
        self.username = cfg["username"]
        self.password = cfg["password"]
        self.sender = cfg.get("sender", self.username)
        self.receiver = cfg["receiver"]

    async def send(self, message: str) -> bool:
        # 邮件是同步操作，用 asyncio 包装
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._send_sync, message)

    def _send_sync(self, message: str) -> bool:
        try:
            msg = MIMEMultipart()
            msg["From"] = self.sender
            msg["To"] = self.receiver
            msg["Subject"] = "森空岛签到通知"

            # 将换行转为 HTML <br> 以保持格式
            html_body = message.replace("\n", "<br>")
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            if self.use_ssl:
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port)
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port)
                server.starttls()

            server.login(self.username, self.password)
            server.sendmail(self.sender, [self.receiver], msg.as_string())
            server.quit()
            logger.info("[Email] 推送成功")
            return True
        except Exception as e:
            logger.error(f"[Email] 推送失败: {e}")
            return False


# ==================== 企业微信 Webhook ====================
class WeComNotifier(BaseNotifier):
    name = "WeCom"

    def __init__(self, cfg: dict):
        self.webhook_url = cfg["webhook_url"]

    async def send(self, message: str) -> bool:
        payload = {
            "msgtype": "text",
            "text": {"content": message}
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(self.webhook_url, json=payload)
            result = resp.json()
            if result.get("errcode") == 0:
                logger.info("[WeCom] 推送成功")
                return True
            else:
                logger.error(f"[WeCom] 推送失败: {result.get('errmsg')}")
                return False


# ==================== 微信服务号 (公众号模板消息) ====================
class WeChatMPNotifier(BaseNotifier):
    name = "WeChatMP"

    def __init__(self, cfg: dict):
        self.app_id = cfg["app_id"]
        self.app_secret = cfg["app_secret"]
        self.template_id = cfg["template_id"]
        self.open_id = cfg["open_id"]

    async def _get_access_token(self) -> str:
        url = "https://api.weixin.qq.com/cgi-bin/token"
        params = {
            "grant_type": "client_credential",
            "appid": self.app_id,
            "secret": self.app_secret,
        }
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params)
            data = resp.json()
            if "access_token" in data:
                return data["access_token"]
            raise Exception(f"获取access_token失败: {data}")

    async def send(self, message: str) -> bool:
        try:
            access_token = await self._get_access_token()
            url = f"https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={access_token}"

            # 模板消息，将内容放入 first 和 remark 字段
            # 用户需根据自己的模板调整 data 字段
            lines = message.split("\n")
            title = lines[0] if lines else "签到通知"
            content = "\n".join(lines[1:]) if len(lines) > 1 else ""

            payload = {
                "touser": self.open_id,
                "template_id": self.template_id,
                "data": {
                    "first": {"value": title, "color": "#173177"},
                    "keyword1": {"value": content[:200], "color": "#173177"},
                    "remark": {"value": content[200:] if len(content) > 200 else "签到完成", "color": "#999999"},
                }
            }

            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload)
                result = resp.json()
                if result.get("errcode") == 0:
                    logger.info("[WeChatMP] 推送成功")
                    return True
                else:
                    logger.error(f"[WeChatMP] 推送失败: {result.get('errmsg')}")
                    return False
        except Exception as e:
            logger.error(f"[WeChatMP] 推送异常: {e}")
            return False


# ==================== Server酱 ====================
class ServerChanNotifier(BaseNotifier):
    name = "ServerChan"

    def __init__(self, cfg: dict):
        self.send_key = cfg["send_key"]

    async def send(self, message: str) -> bool:
        url = f"https://sctapi.ftqq.com/{self.send_key}.send"
        lines = message.split("\n")
        title = lines[0] if lines else "森空岛签到通知"

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, data={
                "title": title,
                "desp": message,
            })
            result = resp.json()
            if result.get("code") == 0:
                logger.info("[ServerChan] 推送成功")
                return True
            else:
                logger.error(f"[ServerChan] 推送失败: {result.get('message')}")
                return False
