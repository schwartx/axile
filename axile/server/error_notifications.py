"""
服务端错误通知辅助函数.

集中封装飞书错误卡片构建、外部 IP 获取与错误上报流程。
"""

from __future__ import annotations

import asyncio
import traceback
import uuid

import aiohttp
import loguru

from axile.common.config import settings
from axile.common.feishu import push_feishu_card
from axile.common.notification_context import ACCOUNT_CONTROL_WARNING_EVENT
from axile.common.notification_function import NotificationFunctionResult, run_notification_function
from axile.common.quota_warning_card import build_quota_warning_card
from axile.executor.algorithms.utils.clock import clock_now
from axile.server.db.models.account import AccountContext
from axile.server.db.models.system_notification import SystemNotificationStatePublic
from axile.server.execution.notification_state import record_system_notification_result


def build_error_card(
    err: str,
    account_name: str | None = None,
    external_ip: str | None = None,
) -> dict[str, object]:
    """
    构建飞书错误通知卡片.

    Parameters
    ----------
    err : str
        需要展示的错误详情。
    account_name : str | None, optional
        当前执行对应的账户名称。
    external_ip : str | None, optional
        当前服务实例的外部 IP。

    Returns
    -------
    dict[str, object]
        可直接发送到飞书的卡片 JSON。
    """
    current_time = clock_now().strftime("%Y-%m-%d %H:%M:%S")
    info_sections = [f"**⏰ 发生时间:** {current_time}"]
    if account_name:
        info_sections.append(f"**👤 账户名称:** {account_name}")
    else:
        info_sections.append("**👤 账户名称:** 未指定")
    if external_ip:
        info_sections.append(f"**🌐 外部IP:** {external_ip}")
    else:
        info_sections.append("**🌐 外部IP:** **无法获取IP**")

    info_content = "  \n".join(info_sections)
    error_lines = err.strip().split("\n")
    error_preview = error_lines[0] if error_lines else "未知错误"
    full_error = err.strip()

    return {
        "header": {
            "title": {"tag": "plain_text", "content": "🚨 系统错误通知"},
            "template": "red",
        },
        "elements": [
            {"tag": "markdown", "content": f"📋 **基本信息**\n{info_content}"},
            {"tag": "hr"},
            {"tag": "markdown", "content": f"🔴 **错误预览:**\n {error_preview}"},
            {"tag": "markdown", "content": f"📝 **详细信息**\n\n```\n{full_error}\n```"},
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": "⚠️ **处理建议**\n请立即检查系统状态，确认错误原因并及时处理。\n<at id=all></at> **请关注此错误**",
            },
        ],
    }


def build_test_card() -> dict[str, object]:
    """
    构建飞书告警联通测试卡片.

    Returns
    -------
    dict[str, object]
        结构与 :func:`build_error_card` 一致（``header`` + ``markdown`` 元素），
        以便「测试推送」验证真实告警所用的同一套卡片契约；模板与文案改为中性提示，
        避免收信人误认为真实错误。

    Notes
    -----
    仅用于系统配置向导的连通性自检，不含任何错误详情。
    """
    current_time = clock_now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "header": {
            "title": {"tag": "plain_text", "content": "🔔 axile 告警联通测试"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    "若你在群里看到这张卡片，说明**执行错误告警**的飞书机器人 key 配置正确。\n\n"
                    f"**⏰ 测试时间:** {current_time}\n\n"
                    "此为 axile 系统配置向导发出的联通测试，可忽略。"
                ),
            },
        ],
    }


async def get_external_ip() -> str:
    """
    获取当前服务实例的外部 IP 地址.

    Returns
    -------
    str
        获取成功时返回 IPv4 地址；所有服务都失败时返回空字符串。
    """
    ip_services = [
        "https://ip.sb",
        "https://ifconfig.me",
        "https://ipinfo.io/ip",
        "http://myip.ipip.net",
        "http://ip.6655.com/ip.aspx",
        "https://cip.cc",
    ]
    timeout = aiohttp.ClientTimeout(total=5)

    for service in ip_services:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(service) as response:
                    if response.status != 200:
                        continue

                    ip_text = await response.text()
                    ip = ip_text.strip()
                    if service == "https://cip.cc":
                        continue
                    if service == "http://myip.ipip.net":
                        import re

                        match = re.search(r"(\d+\.\d+\.\d+\.\d+)", ip)
                        if match:
                            ip = match.group(1)

                    if ip and "." in ip and len(ip.split(".")) == 4:
                        parts = ip.split(".")
                        if all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
                            return ip
        except asyncio.TimeoutError:
            loguru.logger.warning(f"获取外部IP超时: {service}")
        except Exception as exc:
            loguru.logger.warning(f"获取外部IP失败 {service}: {exc}")

    loguru.logger.warning("所有IP服务都无法获取外部IP地址")
    return ""


async def send_feishu_error(
    error: Exception,
    account: AccountContext | None,
    feishu_key: str,
    *,
    event_type: str = "execution_error",
    execution_id: str | None = None,
) -> None:
    """
    将执行异常发送到飞书.

    Parameters
    ----------
    error : Exception
        需要上报的异常对象。
    account : Account | None
        当前执行关联的账户对象。
    feishu_key : str
        飞书机器人 webhook key。
    """
    mode = settings.system_execution_notification_mode
    if mode == "default" and not feishu_key:
        return
    context = build_system_notification_context(error, account, event_type=event_type, execution_id=execution_id)
    result = await _send_system_notification(mode, context, error, account, feishu_key)
    await _record_system_result(context, mode, result)


async def send_system_notification(context: dict[str, object]) -> NotificationFunctionResult:
    """复用系统告警配置发送额度预警；默认缺少凭据时如实记录失败。"""
    mode = settings.system_execution_notification_mode
    key = settings.exe_err_feishu_key or ""
    result = (
        NotificationFunctionResult(False, "未配置系统飞书 Webhook，额度预警未发送")
        if mode == "default" and not key
        else await _send_system_notification(mode, context, None, None, key)
    )
    await _record_system_result(context, mode, result)
    return result


async def _record_system_result(context: dict, mode: str, result: NotificationFunctionResult) -> None:
    """系统告警共用发送摘要，保存失败不改变原发送结果。"""
    account = context.get("account") or {}
    state = SystemNotificationStatePublic(
        finished_at=clock_now().isoformat(),
        mode=mode,
        event_type=context["event_type"],
        execution_id=context.get("execution_id"),
        account_id=account.get("id"),
        ok=result.ok,
        error=result.error,
    )
    try:
        await asyncio.to_thread(record_system_notification_result, state)
    except Exception as exc:
        # 摘要写入失败不能反过来改变交易或发送结果。
        loguru.logger.warning("保存系统告警结果失败: {}", exc)
    loguru.logger.info("系统执行通知完成 execution_id={} mode={} ok={}", state.execution_id, mode, result.ok)


def build_system_notification_context(
    error: Exception,
    account: AccountContext | None,
    *,
    event_type: str = "execution_error",
    execution_id: str | None = None,
    is_test: bool = False,
) -> dict[str, object]:
    """真实告警与试跑共用事件结构；堆栈来自异常对象而非当前 except 栈。"""
    return {
        "event_id": f"system:{execution_id}:{event_type}" if execution_id else f"system:{uuid.uuid4().hex}",
        "event_type": event_type,
        "execution_id": execution_id,
        "occurred_at": clock_now().isoformat(),
        "account": {"id": account.id, "name": account.name} if account else None,
        "error": {
            "type": type(error).__name__,
            "message": str(error),
            "traceback": "".join(traceback.format_exception(error)),
        },
        "is_test": is_test,
    }


async def _send_system_notification(
    mode: str,
    context: dict[str, object],
    error: Exception | None,
    account: AccountContext | None,
    feishu_key: str,
) -> NotificationFunctionResult:
    """两种发送路径返回相同结果；失败只记通知错误，不补发另一模式。"""
    try:
        if mode == "function":
            result = await asyncio.to_thread(
                run_notification_function,
                settings.system_execution_notification_code,
                context,
                system_feishu_key=feishu_key,
            )
            if not result.ok:
                loguru.logger.error(f"自定义系统执行通知失败: {result.error}")
            return result
        if context["event_type"] == ACCOUNT_CONTROL_WARNING_EVENT:
            card = build_quota_warning_card(context)
        else:
            assert error is not None
            error_msg = f"{error}\n\n堆栈跟踪:\n{''.join(traceback.format_exception(error))}"
            external_ip = await get_external_ip()
            card = build_error_card(error_msg, account.name if account else None, external_ip)
        await asyncio.to_thread(push_feishu_card, card, feishu_key)
        return NotificationFunctionResult(True)
    except Exception as exc:
        loguru.logger.error(f"发送飞书错误通知失败: {exc}")
        return NotificationFunctionResult(False, str(exc)[:1000])
