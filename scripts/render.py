#!/usr/bin/env python3
"""Renderiza los SVG del perfil (terminal SRGO) a partir de la actividad real en GitHub.

Sin dependencias externas: solo biblioteca estandar.
Uso:  python scripts/render.py [--login USER] [--out assets] [--offline data.json]
Token: variable de entorno GITHUB_TOKEN (opcional en modo --offline).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from html import escape

QUERY = """
query($login: String!) {
  user(login: $login) {
    createdAt
    followers { totalCount }
    repositories(first: 100, affiliations: OWNER, orderBy: {field: PUSHED_AT, direction: DESC}) {
      totalCount
      nodes {
        name isFork isPrivate stargazerCount
        languages(first: 8, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name } } }
      }
    }
    contributionsCollection {
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""

MONO = "'IBM Plex Mono','JetBrains Mono','Cascadia Code','SF Mono',Menlo,Consolas,'DejaVu Sans Mono',monospace"

THEMES = {
    "dark": dict(
        bg="#000000", frame="#1C1C1C", grid="#161616",
        ink="#FFB000", dim="#8A6100", mute="#5C4100", text="#E8E2D0",
        up="#2BD576", down="#FF4D4D", ema="#6FB7FF", tag="#FFB000", tagtext="#000000",
    ),
    "light": dict(
        bg="#F3F1EB", frame="#CFC9B9", grid="#E3DFD2",
        ink="#9A5B00", dim="#B08A4A", mute="#A8946A", text="#1E1B16",
        up="#118A4A", down="#C8322B", ema="#2559B8", tag="#9A5B00", tagtext="#FFFFFF",
    ),
}

MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def fmt(n: int | float) -> str:
    return f"{int(round(n)):,}".replace(",", ".")


def fetch(login: str, token: str) -> dict:
    body = json.dumps({"query": QUERY, "variables": {"login": login}}).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql", data=body,
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "srgo-terminal-render"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    if "errors" in data:
        raise SystemExit(f"GraphQL: {data['errors']}")
    return data["data"]["user"]


# ----------------------------------------------------------------------------- datos

def build_series(user: dict) -> dict:
    weeks_all = user["contributionsCollection"]["contributionCalendar"]["weeks"]
    days = [d for w in weeks_all for d in w["contributionDays"]]
    counts = [d["contributionCount"] for d in days]
    dates = [dt.date.fromisoformat(d["date"]) for d in days]
    # Indice SRGO: contribuciones de los ultimos 7 dias (ventana movil)
    idx = [sum(counts[max(0, i - 6): i + 1]) for i in range(len(counts))]

    pos = {d["date"]: i for i, d in enumerate(days)}
    candles = []
    for w in weeks_all[-52:]:
        ids = [pos[d["date"]] for d in w["contributionDays"]]
        first, last = ids[0], ids[-1]
        o = idx[first - 1] if first > 0 else idx[first]
        c = idx[last]
        hs = idx[first:last + 1] + [o]
        candles.append(dict(
            start=dates[first], end=dates[last], open=o, close=c,
            high=max(hs), low=min(hs), vol=sum(counts[first:last + 1]),
        ))

    # EMA de 4 semanas sobre cierres
    k = 2 / (4 + 1)
    ema: list[float] = []
    for i, cd in enumerate(candles):
        ema.append(cd["close"] if i == 0 else cd["close"] * k + ema[-1] * (1 - k))

    # rachas
    cur = 0
    for i in range(len(counts) - 1, -1, -1):
        if counts[i] > 0:
            cur += 1
        elif i == len(counts) - 1:
            continue  # hoy todavia puede sumar
        else:
            break
    best = run = 0
    for c in counts:
        run = run + 1 if c > 0 else 0
        best = max(best, run)

    best_week = max(candles, key=lambda c: c["vol"])
    return dict(
        candles=candles, ema=ema, streak=cur, best_streak=best,
        total=user["contributionsCollection"]["contributionCalendar"]["totalContributions"],
        commits=user["contributionsCollection"]["totalCommitContributions"],
        high52=max(c["high"] for c in candles), best_week=best_week,
        active_days=sum(1 for c in counts if c > 0),
        last_date=dates[-1],
    )


def build_languages(user: dict) -> list[tuple[str, int]]:
    agg: dict[str, int] = {}
    for r in user["repositories"]["nodes"]:
        if r["isFork"]:
            continue
        for e in r["languages"]["edges"]:
            agg[e["node"]["name"]] = agg.get(e["node"]["name"], 0) + e["size"]
    return sorted(agg.items(), key=lambda kv: -kv[1])[:7]


# ----------------------------------------------------------------------------- svg

def head(w: int, h: int, t: dict, extra_css: str = "") -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" font-family="{MONO}">\n'
        f"<style>\n"
        f"text{{fill:{t['text']}}}\n"
        f".ink{{fill:{t['ink']}}} .dim{{fill:{t['dim']}}} .mute{{fill:{t['mute']}}} "
        f".up{{fill:{t['up']}}} .down{{fill:{t['down']}}}\n"
        f"{extra_css}\n"
        f"@media (prefers-reduced-motion: reduce){{*{{animation:none !important}}}}\n"
        f"</style>\n"
        f'<rect width="{w}" height="{h}" rx="6" fill="{t["bg"]}"/>\n'
        f'<rect x="0.5" y="0.5" width="{w-1}" height="{h-1}" rx="6" fill="none" stroke="{t["frame"]}"/>\n'
    )


def mono_text(x, y, s, size, cls="", anchor="start", weight=None, extra="") -> str:
    # textLength fija el ancho para que la fuente del visitante no rompa la maqueta
    w = len(s) * size * 0.6
    wt = f' font-weight="{weight}"' if weight else ""
    c = f' class="{cls}"' if cls else ""
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}"{wt}{c} '
            f'textLength="{w:.1f}" lengthAdjust="spacingAndGlyphs" {extra}>{escape(s)}</text>\n')


def render_header(t: dict) -> str:
    W, H = 900, 236
    phrases = [
        "Construyo sistemas de IA con datos reales, trazabilidad y control humano.",
        "Trading algoritmico, triaje AML multiagente y LLM autoalojados.",
        "Buscando mi primera experiencia profesional en IA aplicada.",
    ]
    tickers = [
        ("PYTHON", "+3.11"), ("LIGHTGBM", "+1.02"), ("SKLEARN", "+0.87"), ("DJANGO", "+0.42"),
        ("DOTNET", "+0.18"), ("POSTGRES", "+0.63"), ("AZURE", "+0.29"), ("STREAMLIT", "+0.55"),
        ("PANDAS", "+2.40"), ("NETWORKX", "+0.31"), ("OLLAMA", "+0.77"), ("MT5", "+0.12"),
    ]
    size = 15
    cw = size * 0.6
    per = 12.0  # segundos por frase
    total = per * len(phrases)
    css = [
        ".blink{animation:blink 1.1s steps(2) infinite} @keyframes blink{to{opacity:0}}",
        ".tape{animation:tape 42s linear infinite} @keyframes tape{to{transform:translateX(-50%)}}",
    ]
    out = []
    # barra superior
    out.append(f'<rect x="1" y="1" width="{W-2}" height="30" fill="{t["frame"]}" opacity="0.55"/>')
    out.append(mono_text(18, 21, "SRGO", 13, "ink", weight=700))
    out.append(mono_text(66, 21, "Sergio Sebastian Ascurra Perez", 13))
    out.append(mono_text(W - 18, 21, "LIMA, PE  ·  USMP  ·  IA APLICADA", 12, "dim", anchor="end"))
    # cuadricula tenue
    for gx in range(40, W - 20, 40):
        out.append(f'<line x1="{gx}" y1="31" x2="{gx}" y2="{H-44}" stroke="{t["grid"]}"/>')
    # nombre grande
    out.append(mono_text(28, 92, "SERGIO ASCURRA", 44, "ink", weight=700))
    out.append(mono_text(30, 118, "Ingenieria de Computacion y Sistemas  //  IA aplicada y desarrollo de software", 13, "dim"))
    # prompt con frases que se escriben solas
    out.append(mono_text(30, 160, ">", 15, "ink", weight=700))
    out.append(f'<rect class="blink" x="46" y="148" width="9" height="15" fill="{t["ink"]}"/>')
    out.append("<defs>")
    for i, p in enumerate(phrases):
        w = len(p) * cw
        d = i * per
        # 0 -> escribe (3s) -> mantiene -> se borra justo antes de la siguiente
        css.append(
            f".t{i}{{animation:type{i} {total}s steps({len(p)}) infinite;animation-delay:{d}s}}"
            f"@keyframes type{i}{{0%{{width:0}}{3/total*100:.3f}%{{width:{w:.0f}px}}"
            f"{(per-0.4)/total*100:.3f}%{{width:{w:.0f}px}}{(per-0.2)/total*100:.3f}%{{width:0}}100%{{width:0}}}}"
        )
        out.append(f'<clipPath id="c{i}"><rect class="t{i}" x="62" y="144" width="0" height="22"/></clipPath>')
    out.append("</defs>")
    for i, p in enumerate(phrases):
        out.append(f'<g clip-path="url(#c{i})">' + mono_text(62, 160, p, size) + "</g>")
    # franja de indicadores
    chips = [("1.er puesto", "Hack AI Agents Impact 2026"), ("MIT Open Learning", "Universal AI Program"), ("USMP", "8.o ciclo")]
    x = 30.0
    for a, b in chips:
        wa = len(a) * 11 * 0.6 + 14
        out.append(f'<rect x="{x:.0f}" y="176" width="{wa:.0f}" height="20" rx="3" fill="{t["ink"]}"/>')
        out.append(mono_text(x + 7, 190, a, 11, weight=700, extra=f'fill="{t["tagtext"]}"'))
        out.append(mono_text(x + wa + 8, 190, b, 11, "dim"))
        x += wa + 8 + len(b) * 11 * 0.6 + 22
    # cinta de cotizaciones
    out.append(f'<line x1="1" y1="{H-40}" x2="{W-1}" y2="{H-40}" stroke="{t["frame"]}"/>')
    items = []
    tx = 0.0
    for name, chg in tickers:
        items.append(mono_text(tx, H - 15, name, 12, "ink", weight=700))
        tx += len(name) * 7.2 + 8
        items.append(mono_text(tx, H - 15, "▲", 10, "up"))
        tx += 14
        items.append(mono_text(tx, H - 15, chg, 12, "up"))
        tx += len(chg) * 7.2 + 26
    half = tx
    inner = "".join(items)
    out.append(f'<clipPath id="tape"><rect x="1" y="{H-40}" width="{W-2}" height="39"/></clipPath>')
    out.append(f'<g clip-path="url(#tape)"><g class="tape">{inner}<g transform="translate({half:.1f} 0)">{inner}</g></g></g>')
    return head(W, H, t, "\n".join(css)) + "".join(out) + "</svg>\n"


def render_chart(t: dict, s: dict) -> str:
    W, H = 900, 372
    L, R, TOP, BOT = 30, 84, 58, 262   # area de velas
    VT, VB = 274, 316                   # area de volumen
    cs = s["candles"]
    n = len(cs)
    slot = (W - L - R) / n
    bw = max(6, slot * 0.58)
    ymax = max(1, s["high52"]) * 1.08
    vmax = max(1, max(c["vol"] for c in cs))

    def Y(v):
        return BOT - (v / ymax) * (BOT - TOP)

    css = [
        ".c{animation:rise .45s cubic-bezier(.2,.8,.2,1) both}",
        "@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}",
        ".ema{stroke-dasharray:1;stroke-dashoffset:1;animation:draw 1.6s ease-out 1.3s forwards}",
        "@keyframes draw{to{stroke-dashoffset:0}}",
        ".late{opacity:0;animation:fade .6s ease-out 2.6s forwards} @keyframes fade{to{opacity:1}}",
        ".pulse{animation:pulse 2.4s ease-in-out 3.2s infinite} @keyframes pulse{50%{opacity:.35}}",
    ]
    last, prev = cs[-1], cs[-2]
    chg = last["close"] - prev["close"]
    pct = (chg / prev["close"] * 100) if prev["close"] else (100.0 if chg else 0.0)
    arrow, cls = ("▲", "up") if chg >= 0 else ("▼", "down")
    out = []
    out.append(f'<rect x="1" y="1" width="{W-2}" height="30" fill="{t["frame"]}" opacity="0.55"/>')
    out.append(mono_text(18, 21, "SRGO/GH", 13, "ink", weight=700))
    out.append(mono_text(90, 21, "Indice de contribucion  ·  velas semanales  ·  52 semanas", 12, "dim"))
    out.append(mono_text(W - 18, 21, f"ult. actualizacion {s['last_date'].isoformat()}", 11, "dim", anchor="end"))
    # precio actual
    out.append(mono_text(L, 48, f"{fmt(last['close'])}", 26, "ink", weight=700))
    px = L + len(fmt(last["close"])) * 26 * 0.6 + 10
    out.append(mono_text(px, 48, f"{arrow} {chg:+d}  ({pct:+.1f}%) vs semana anterior", 12, cls))
    out.append(mono_text(W - R - 6, 48, "SRGO = contribuciones en los ultimos 7 dias", 11, "mute", anchor="end"))
    # rejilla y eje Y
    steps = 4
    for i in range(steps + 1):
        v = ymax * i / steps
        y = Y(v)
        out.append(f'<line x1="{L}" y1="{y:.1f}" x2="{W-R}" y2="{y:.1f}" stroke="{t["grid"]}"/>')
        out.append(mono_text(W - R + 10, y + 4, fmt(v), 11, "dim"))
    # eje X: meses
    seen = set()
    for i, c in enumerate(cs):
        m = (c["start"].year, c["start"].month)
        if m not in seen and c["start"].day <= 7:
            seen.add(m)
            x = L + i * slot
            out.append(f'<line x1="{x:.1f}" y1="{BOT}" x2="{x:.1f}" y2="{VB}" stroke="{t["grid"]}"/>')
            out.append(mono_text(x + 3, VB + 14, MESES[c["start"].month - 1], 11, "dim"))
    # volumen
    for i, c in enumerate(cs):
        x = L + i * slot + (slot - bw) / 2
        h = (c["vol"] / vmax) * (VB - VT)
        col = t["up"] if c["close"] >= c["open"] else t["down"]
        out.append(f'<rect class="c" style="animation-delay:{i*22}ms" x="{x:.1f}" y="{VB-h:.1f}" '
                   f'width="{bw:.1f}" height="{h:.1f}" fill="{col}" opacity="0.35"/>')
    out.append(mono_text(L, VT + 10, "vol. semanal", 10, "mute"))
    # velas
    for i, c in enumerate(cs):
        x0 = L + i * slot
        xm = x0 + slot / 2
        col = t["up"] if c["close"] >= c["open"] else t["down"]
        top, bot = Y(max(c["open"], c["close"])), Y(min(c["open"], c["close"]))
        if bot - top < 1.5:
            bot = top + 1.5
        out.append(
            f'<g class="c" style="animation-delay:{i*22}ms">'
            f'<line x1="{xm:.1f}" y1="{Y(c["high"]):.1f}" x2="{xm:.1f}" y2="{Y(c["low"]):.1f}" stroke="{col}" stroke-width="1.2"/>'
            f'<rect x="{x0 + (slot-bw)/2:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{bot-top:.1f}" fill="{col}"/></g>'
        )
    # EMA 4
    pts = " ".join(f"{L + i*slot + slot/2:.1f},{Y(v):.1f}" for i, v in enumerate(s["ema"]))
    out.append(f'<polyline class="ema" pathLength="1" points="{pts}" fill="none" stroke="{t["ema"]}" '
               f'stroke-width="1.6" stroke-linejoin="round"/>')
    out.append(mono_text(L, TOP + 2, "— EMA 4 semanas", 10, extra=f'fill="{t["ema"]}"'))
    # etiqueta de ultimo precio
    yl = Y(last["close"])
    out.append(f'<g class="late"><line x1="{L}" y1="{yl:.1f}" x2="{W-R}" y2="{yl:.1f}" stroke="{t["tag"]}" '
               f'stroke-dasharray="3 4" opacity="0.6"/>'
               f'<rect class="pulse" x="{W-R+4}" y="{yl-9:.1f}" width="{R-10}" height="18" rx="2" fill="{t["tag"]}"/>'
               + mono_text(W - R + 4 + (R - 10) / 2, yl + 4, fmt(last["close"]), 11, anchor="middle", weight=700,
                           extra=f'fill="{t["tagtext"]}"') + "</g>")
    # fila de estadisticas
    bw_ = s["best_week"]
    stats = [
        ("total 12 meses", fmt(s["total"])),
        ("max. 52 sem.", fmt(s["high52"])),
        ("mejor semana", f"{fmt(bw_['vol'])}  ({bw_['start'].day} {MESES[bw_['start'].month-1]})"),
        ("racha actual", f"{s['streak']} d"),
        ("racha max.", f"{s['best_streak']} d"),
        ("dias activos", f"{s['active_days']}/365"),
    ]
    out.append(f'<line x1="1" y1="{H-36}" x2="{W-1}" y2="{H-36}" stroke="{t["frame"]}"/>')
    x = 18.0
    for k, v in stats:
        out.append(mono_text(x, H - 14, k, 10, "dim"))
        x += len(k) * 6 + 8
        out.append(mono_text(x, H - 14, v, 11, "ink", weight=700))
        x += len(v) * 6.6 + 20
    return head(W, H, t, "\n".join(css)) + "".join(out) + "</svg>\n"


def render_langs(t: dict, langs: list[tuple[str, int]], user: dict) -> str:
    rows = len(langs)
    W, H = 900, 74 + rows * 26 + 20
    total = sum(v for _, v in langs) or 1
    css = [
        ".bar{transform-origin:left;animation:grow .9s cubic-bezier(.2,.8,.2,1) both} "
        "@keyframes grow{from{transform:scaleX(0)}}",
    ]
    out = []
    out.append(f'<rect x="1" y="1" width="{W-2}" height="30" fill="{t["frame"]}" opacity="0.55"/>')
    out.append(mono_text(18, 21, "LIBRO DE ORDENES", 13, "ink", weight=700))
    out.append(mono_text(170, 21, "lenguajes por bytes en repositorios propios", 12, "dim"))
    repos = user["repositories"]["totalCount"]
    out.append(mono_text(W - 18, 21, f"{repos} repos  ·  {user['followers']['totalCount']} seguidores", 11, "dim", anchor="end"))
    out.append(mono_text(30, 56, "lenguaje", 10, "mute"))
    out.append(mono_text(160, 56, "profundidad", 10, "mute"))
    out.append(mono_text(W - 150, 56, "bytes", 10, "mute", anchor="end"))
    out.append(mono_text(W - 30, 56, "peso", 10, "mute", anchor="end"))
    barL, barR = 160, W - 260
    for i, (name, size) in enumerate(langs):
        y = 80 + i * 26
        pct = size / total * 100
        w = (barR - barL) * size / langs[0][1]
        out.append(mono_text(30, y, name, 12, "ink" if i == 0 else "", weight=700 if i == 0 else None))
        out.append(f'<rect x="{barL}" y="{y-11}" width="{barR-barL}" height="14" fill="{t["grid"]}"/>')
        out.append(f'<rect class="bar" style="animation-delay:{i*90}ms" x="{barL}" y="{y-11}" width="{w:.1f}" height="14" '
                   f'fill="{t["up"] if i == 0 else t["ink"]}" opacity="{1 if i == 0 else 0.85 - i*0.08:.2f}"/>')
        out.append(mono_text(W - 150, y, fmt(size), 11, "dim", anchor="end"))
        out.append(mono_text(W - 30, y, f"{pct:5.1f}%", 12, anchor="end"))
    return head(W, H, t, "\n".join(css)) + "".join(out) + "</svg>\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", default="SergioAscurraPerez")
    ap.add_argument("--out", default="assets")
    ap.add_argument("--offline", help="JSON guardado de la API (para pruebas)")
    ap.add_argument("--dump", help="guarda la respuesta de la API en este archivo")
    a = ap.parse_args()

    if a.offline:
        with open(a.offline, encoding="utf-8") as f:
            raw = json.load(f)
        user = raw["data"]["user"] if "data" in raw else raw
    else:
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            sys.exit("Falta GITHUB_TOKEN")
        user = fetch(a.login, token)
        if a.dump:
            with open(a.dump, "w", encoding="utf-8") as f:
                json.dump({"data": {"user": user}}, f)

    series = build_series(user)
    langs = build_languages(user)
    os.makedirs(a.out, exist_ok=True)
    for name, t in THEMES.items():
        files = {
            f"header-{name}.svg": render_header(t),
            f"chart-{name}.svg": render_chart(t, series),
            f"langs-{name}.svg": render_langs(t, langs, user),
        }
        for fn, svg in files.items():
            with open(os.path.join(a.out, fn), "w", encoding="utf-8", newline="\n") as f:
                f.write(svg)
            print("ok", fn, f"{len(svg)//1024} KB")


if __name__ == "__main__":
    main()
