"""Application PC Pokémon Deals : tableau de bord local + scans automatiques + alertes Telegram."""
import asyncio
import logging
import os
import subprocess
import sys
import traceback
import webbrowser
from pathlib import Path

import httpx

from aiohttp import web

from pokedeals import assistant, telegram_bot
from pokedeals import web as webapp
from pokedeals.config import Config
from pokedeals.engine import Engine

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
for noisy in ("httpx", "apscheduler", "aiohttp.access", "telegram"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


def load_config() -> Config:
    cfg = Config.from_env()
    required = {"TELEGRAM_TOKEN": cfg.telegram_token, "EBAY_CLIENT_ID": cfg.ebay_client_id,
                "EBAY_CLIENT_SECRET": cfg.ebay_client_secret,
                "TELEGRAM_CHAT_ID": str(cfg.telegram_chat_id or "")}
    if all(required.values()):
        return cfg
    if not sys.stdin.isatty():
        missing = [name for name, value in required.items() if not value]
        raise SystemExit(f"Variables manquantes dans .env : {', '.join(missing)}")
    assistant.run(required)
    for key in required:
        os.environ.pop(key, None)
    return Config.from_env()


HOST = "127.0.0.1"  # plutôt que « localhost », que certains Windows envoient vers IPv6


def open_app(url: str) -> None:
    """Ouvre l'application dans une fenêtre dédiée (Edge ou Chrome en mode application), sinon un onglet."""
    if sys.platform == "win32":
        candidates = [
            Path(os.environ.get(var, "")) / sub
            for var in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")
            for sub in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe")
        ]
        for exe in candidates:
            if exe.is_file():
                subprocess.Popen([str(exe), f"--app={url}", "--window-size=1280,900"])
                return
    webbrowser.open(url)


async def responds(url: str) -> bool:
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            return (await client.get(f"{url}/api/status")).status_code == 200
    except httpx.HTTPError:
        return False


async def start_server(engine: Engine) -> tuple[web.AppRunner, str]:
    """Démarre l'interface sur le premier port libre à partir de WEB_PORT."""
    runner = web.AppRunner(webapp.build(engine))
    await runner.setup()
    for port in range(engine.cfg.web_port, engine.cfg.web_port + 10):
        try:
            await web.TCPSite(runner, HOST, port).start()
        except OSError:
            continue
        try:
            await web.TCPSite(runner, "::1", port).start()  # pour les navigateurs qui essaient IPv6 d'abord
        except OSError:
            pass
        engine.cfg.web_port = port
        return runner, f"http://{HOST}:{port}"
    raise SystemExit(f"Aucun port libre entre {engine.cfg.web_port} et {engine.cfg.web_port + 9}.")


async def main() -> None:
    cfg = load_config()
    existing = f"http://{HOST}:{cfg.web_port}"
    if await responds(existing):
        print(f"L'application tourne déjà : ouverture de {existing}", flush=True)
        open_app(existing)
        return

    engine = Engine(cfg)
    runner, url = await start_server(engine)
    for _ in range(20):  # on n'ouvre la fenêtre qu'une fois le serveur prêt
        if await responds(url):
            break
        await asyncio.sleep(0.25)

    bot = telegram_bot.build(engine)
    if bot:
        try:
            await bot.initialize()
            await bot.start()
            await bot.updater.start_polling()
        except Exception as exc:
            print(f"Telegram injoignable ({exc}) : l'application tourne sans alertes Telegram.", flush=True)
            engine.notifiers.clear()
            bot = None

    print("=" * 60)
    print(f"  Application ouverte : {url}")
    print("  Si elle ne s'affiche pas, copie cette adresse dans ton navigateur.")
    print("  Laisse cette fenêtre ouverte pour que les scans continuent.")
    print("=" * 60, flush=True)
    open_app(url)

    try:
        await engine.run_forever()
    finally:
        if bot:
            await bot.updater.stop()
            await bot.stop()
            await bot.shutdown()
        await runner.cleanup()
        await engine.close()


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except Exception:
        details = traceback.format_exc()
        (Path(__file__).parent / "erreur.log").write_text(details, encoding="utf-8")
        print(details)
        print("L'application a planté. Envoie une capture de cette fenêtre (ou le fichier erreur.log).")
        raise SystemExit(1)


if __name__ == "__main__":
    run()
