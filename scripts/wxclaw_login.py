import argparse
import asyncio
import json
from pathlib import Path

import nonebot
from nonebot.adapters.wxclaw import Adapter as WxClawAdapter


DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"


async def ask_verify_code() -> str:
    return await asyncio.to_thread(
        input,
        "如果手机微信显示配对数字，请输入后回车；没有显示则直接回车：",
    )


def build_env_line(account_id: str, token: str, base_url: str) -> str:
    accounts = [
        {
            "account_id": account_id,
            "token": token,
            "base_url": base_url or DEFAULT_BASE_URL,
            "enabled": True,
        }
    ]
    return f"WXCLAW_ACCOUNTS='{json.dumps(accounts, ensure_ascii=False)}'"


def write_env_line(env_line: str) -> None:
    env_path = Path(".env")
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    replaced = False

    for index, line in enumerate(lines):
        if line.startswith("WXCLAW_ACCOUNTS="):
            lines[index] = env_line
            replaced = True
            break

    if not replaced:
        lines.append(env_line)

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Login WxClaw account by QR code.")
    parser.add_argument("--write-env", action="store_true", help="write login result to .env")
    args = parser.parse_args()

    nonebot.init()
    driver = nonebot.get_driver()
    driver.register_adapter(WxClawAdapter)

    adapter = driver._adapters.get("WxClaw")
    if not isinstance(adapter, WxClawAdapter):
        raise RuntimeError("WxClaw adapter was not registered correctly.")

    async with adapter.qr_login(
        auto_connect=False,
        verify_code_callback=ask_verify_code,
    ) as session:
        print("请用微信扫描下面的二维码链接：")
        print(session.qrcode_url)
        print("扫码后在手机上确认登录，终端会继续等待结果。")

        result = await session.wait()

    if not result.connected:
        print(f"登录失败：{result.message}")
        return

    env_line = build_env_line(
        account_id=result.account_id,
        token=result.token,
        base_url=result.base_url or DEFAULT_BASE_URL,
    )

    print("登录成功。")
    print("将下面这行配置写入 .env 后，正常启动 bot 即可自动连接微信：")
    print(env_line)

    if args.write_env:
        write_env_line(env_line)
        print("已写入 .env。")


if __name__ == "__main__":
    asyncio.run(main())
