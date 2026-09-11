"""Quick check: does DuckDuckGo web search work on this machine with the app's httpx?
Run inside your venv:  python test_search.py
No ddgs needed — this is the exact fallback the agent uses."""
import asyncio
import html
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx


async def go():
    q = "sportstech sTread pro treadmill review"
    print(f"httpx version: {httpx.__version__}")
    print(f"Searching: {q!r}\n")
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.post("https://html.duckduckgo.com/html/", data={"q": q})
            print("HTTP status:", r.status_code)
            r.raise_for_status()
            body = r.text
    except Exception as e:
        print(f"\nFAILED to reach DuckDuckGo: {type(e).__name__}: {e}")
        print("=> Your server likely has no internet access, or a proxy/firewall is blocking it.")
        return

    pairs = re.findall(r'result__a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.S)
    print("Results found:", len(pairs))
    for href, title in pairs[:5]:
        if href.startswith("//"):
            href = "https:" + href
        uddg = parse_qs(urlparse(href).query).get("uddg")
        real = unquote(uddg[0]) if uddg else href
        title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        print(f"  - {title[:70]}\n    {real[:90]}")

    if not pairs:
        print("\nReached DuckDuckGo but parsed 0 results — DDG may have changed its HTML,")
        print("or returned a captcha page. Tell Claude and we'll adjust the parser.")
    else:
        print("\nOK — web search works. The agent's fallback will return results like these.")


asyncio.run(go())