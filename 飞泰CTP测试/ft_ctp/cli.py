"""飞泰场景统一入口，操控 Axile 联机执行。"""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from axile.executor.algorithms.utils.clock import clock_now_iso

from ft_ctp.api import Api, ApiError
from ft_ctp.config import Config, load_account
from ft_ctp.evidence import Bundle, Redactor, account_lock
from ft_ctp.runner import Conditions, Runner
from ft_ctp.scenarios import dispatch, export_account

SCENARIOS = ("open", "close", "cancel", "stats", "threshold", "invalid", "reject", "pause", "evidence")


def parser():
    argument_parser = argparse.ArgumentParser(description="通过 Axile REST API 联机执行飞泰 CTP 场景")
    argument_parser.add_argument("command", choices=("run", "inspect", "connect", "collect"))
    argument_parser.add_argument("--config", type=Path, required=True, help="本地配置文件，路径相对该文件解析")
    argument_parser.add_argument("--batch", default=None, help="唯一批次；已存在批次禁止再次执行交易")
    argument_parser.add_argument("--scenarios", default="open,close,cancel,stats", help=",".join(SCENARIOS))
    argument_parser.add_argument("--symbol", help="联机必须显式选择测试合约")
    argument_parser.add_argument(
        "--set", action="append", default=[], metavar="KEY=VALUE", help="覆盖非敏感测试参数，可重复"
    )
    argument_parser.add_argument("--execution-id", help="collect 只读收集指定 execution，不重新执行")
    return argument_parser


def run_scenarios(runner, actions):
    stopped = False
    try:
        runner.preflight()
        if any(action != "evidence" for action in actions):
            runner.prepare()
        for number, action in enumerate(actions, 1):
            step = f"{number:02d}-{action}"
            if stopped and action != "evidence":
                runner.bundle.index["scenarios"].append(
                    {"scenario": step, "status": "not_executed", "reason": "前一场景未通过，避免在未知状态继续交易"}
                )
                continue
            try:
                started_at = clock_now_iso()
                row = dispatch(runner, action, step)
            except Conditions as exc:
                row = {"scenario": step, "status": "insufficient_conditions", "reason": str(exc)}
            except ApiError as exc:
                row = {"scenario": step, "status": "unknown", "reason": str(exc), "http_status": exc.status}
            except (Exception, KeyboardInterrupt) as exc:
                row = {"scenario": step, "status": "unknown", "reason": str(exc) or type(exc).__name__}
            row.update(
                batch=runner.bundle.index["batch"],
                account_id=runner.account_id,
                started_at=started_at,
                finished_at=clock_now_iso(),
                expected={
                    "action": action,
                    "symbol": runner.config.symbol,
                    "lots": runner.config.lots,
                    "side": runner.config.side,
                },
                actual_evidence=step,
            )
            runner.bundle.index["scenarios"].append(row)
            runner.bundle.write("index.json", runner.bundle.index)
            stopped = stopped or row["status"] != "passed"
    except (Exception, KeyboardInterrupt) as exc:
        runner.bundle.index["scenarios"].append(
            {"scenario": "preflight", "status": "insufficient_conditions", "reason": str(exc) or type(exc).__name__}
        )
        for number, action in enumerate(actions, 1):
            runner.bundle.index["scenarios"].append(
                {"scenario": f"{number:02d}-{action}", "status": "not_executed", "reason": "前置／准备未完成"}
            )
    finally:
        finish_run(runner)


def finish_run(runner):
    """收敛已知任务、恢复配置并保留可确认的残留与连接恢复证据。"""
    runner.stop_owned()
    runner.restore()
    try:
        if runner.execution_ids and not runner.pending_execution_ids and runner.original is not None:
            connection = runner.call("GET", runner.base + "/connection")
            if connection.get("status") != "ready":
                runner.bundle.write("cleanup/connection-before-recovery.json", connection)
                runner.connect()
                runner.bundle.write("cleanup/connection-recovered.json", {"purpose": "只读核对残留，不重新执行"})
        residue = {
            "orders": runner.call("GET", runner.base + "/orders?refresh=true"),
            "assets": runner.call("POST", runner.base + "/assets/refresh"),
        }
        runner.bundle.write("final-residue.json", residue)
        if runner.original is not None:
            runner.check_preserved(residue["assets"]["assets"])
        if not residue["orders"].get("complete") or residue["orders"].get("data"):
            runner.bundle.index["missing"].append("最终挂单核对不完整或仍有残留，需人工处理")
        if residue["assets"]["assets"].get("source") in {"error", "assumed", "unavailable"}:
            runner.bundle.index["missing"].append("最终资产快照不可信，持仓待确认")
    except Exception as exc:
        runner.bundle.index["missing"].append(f"最终残留核对不完整: {exc}")
    try:
        export_account(runner)
    except Exception as exc:
        runner.bundle.index["missing"].append(f"账户证据导出失败: {exc}")
    runner.bundle.finish()


def selected_config(args):
    """先解析覆盖值再校验，交易合约必须由用户明确选择。"""
    values = json.loads(args.config.read_text())
    overrides = {}
    for setting in args.set:
        key, separator, raw = setting.partition("=")
        if not separator or key not in Config.__dataclass_fields__:
            raise ValueError("--set 必须指定已知配置字段 KEY=VALUE")
        try:
            overrides[key] = json.loads(raw)
        except json.JSONDecodeError:
            overrides[key] = raw
    if args.symbol is not None:
        overrides["symbol"] = args.symbol
    symbol = overrides.get("symbol", values.get("symbol"))
    if args.command == "run" and (not isinstance(symbol, str) or not symbol.strip()):
        raise ValueError("run 必须通过 --symbol、--set symbol=... 或配置文件显式选择测试合约")
    return Config.read(args.config, overrides)


def main(argv=None):
    args = parser().parse_args(argv)
    bundle = None
    try:
        config = selected_config(args)
        if args.command == "inspect":
            account = load_account(config.database, config.account_name)
            print(
                json.dumps(
                    {
                        "id": account["id"],
                        "name": account["name"],
                        "channel": account["trade_channel"],
                        "is_started": account.get("is_started"),
                        "credentials_found": True,
                        "credential_keys": sorted(account["account_config"]),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        actions = args.scenarios.split(",")
        if not actions or any(action not in SCENARIOS for action in actions):
            raise ValueError("不支持的场景名")
        if args.command == "collect" and not args.execution_id:
            raise ValueError("collect 需要 --execution-id，只读收集，不重新发单")
        account = load_account(config.database, config.account_name)
        api = Api(config.api_url, min(config.timeout_seconds, 15))
        redactor = Redactor(account["account_config"])
        batch = args.batch or "ctp-" + uuid4().hex[:12]
        with account_lock(config.results_dir, account["id"]):
            bundle = Bundle(config.results_dir, batch, config.public(), redactor)
            runner = Runner(config, api, bundle, account["id"])
            runner.credentials = account.get("account_config")
            if args.command == "connect":
                runner.connect()
                bundle.index["scenarios"].append({"scenario": "connect", "status": "passed"})
                bundle.finish()
            elif args.command == "collect":
                runner.execution_ids.append(args.execution_id)
                runner.collect(args.execution_id, "collected")
                bundle.index["scenarios"].append(
                    {
                        "scenario": "collect",
                        "status": "passed" if not bundle.index["missing"] else "unknown",
                        "execution_id": args.execution_id,
                    }
                )
                bundle.finish()
            else:
                run_scenarios(runner, actions)
        print(
            json.dumps(
                {
                    "batch": batch,
                    "mode": bundle.index["mode"],
                    "result_dir": str(bundle.path),
                    "scenarios": bundle.index["scenarios"],
                    "missing": bundle.index["missing"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return (
            0
            if all(row["status"] == "passed" for row in bundle.index["scenarios"]) and not bundle.index["missing"]
            else 2
        )
    except (ValueError, OSError, ApiError, Conditions) as exc:
        if bundle:
            bundle.index["scenarios"].append({"scenario": args.command, "status": "unknown", "reason": str(exc)})
            bundle.finish()
        print(
            json.dumps(
                {
                    "error": bundle.redact(str(exc)) if bundle else type(exc).__name__ + ": " + str(exc),
                    "result_dir": str(bundle.path) if bundle else None,
                },
                ensure_ascii=False,
            )
        )
        return 2
