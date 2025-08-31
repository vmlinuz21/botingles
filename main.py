#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import asyncio
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Dispatcher
from aiogram.types import Message, FSInputFile
from aiogram.filters import Command

# ================== Config ==================
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", "data"))
os.makedirs(DATA_DIR, exist_ok=True)

DEFAULT_WPS = 2.5
MIN_LAST_DUR = 1.2

AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".ogg", ".oga", ".aac", ".flac")
TEXT_EXTS  = (".txt", ".srt", ".vtt")

PAGE_SIZE = 100          # elementos por página (paginación de /list y /search)
MSG_BUDGET = 3900        # margen para no pasar el límite de 4096 de Telegram

# ================== Traducción ==================
from deep_translator import GoogleTranslator

def translate_line(text: str) -> str:
    """Traduce una línea (origen autodetectado -> español). Si falla, devuelve el original."""
    if not text.strip():
        return ""
    try:
        return GoogleTranslator(source="auto", target="es").translate(text)
    except Exception:
        return text

# ================== Modelos / Estado ==================
@dataclass
class Cue:
    start: float
    end: float
    text: str

# Índice global: "root/rel/sin_ext" -> {"audio": path, "cues": List[Cue]}
MEDIA_DB: Dict[str, Dict[str, object]] = {}

# ================== Parsers ==================
def normalize_text(t: str) -> str:
    return re.sub(r"<[^>]+>", "", t.replace("\u200b", "").strip())

def parse_ts(s: str) -> float:
    s = s.strip().replace(",", ".")
    parts = s.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(parts[0])

def parse_srt_vtt(content: str) -> List[Cue]:
    lines = content.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    cues: List[Cue] = []
    i = 0
    time_re = re.compile(
        r"(\d{2}:\d{2}:\d{2}[\.,]\d{3}|\d{2}:\d{2}[\.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[\.,]\d{3}|\d{2}:\d{2}[\.,]\d{3})"
    )
    if lines and lines[0].strip().upper().startswith("WEBVTT"):
        lines = lines[1:]
    while i < len(lines):
        m = time_re.search(lines[i].strip())
        if not m and i + 1 < len(lines):
            m = time_re.search(lines[i + 1].strip())
            if m:
                i += 1
        if m:
            st = parse_ts(m.group(1))
            en = parse_ts(m.group(2))
            i += 1
            block = []
            while i < len(lines) and lines[i].strip():
                block.append(lines[i])
                i += 1
            text = normalize_text(" ".join(block))
            if en > st and text:
                cues.append(Cue(st, en, text))
        i += 1
    return sorted(cues, key=lambda c: c.start)

def parse_txt(content: str) -> List[Cue]:
    rows = [r for r in content.replace("\r\n", "\n").replace("\r", "\n").splitlines() if r.strip()]
    cues: List[Cue] = []
    t0 = 0.0
    for r in rows:
        txt = normalize_text(r)
        # estimación simple de duración por WPS (para conservar orden)
        dur = max(MIN_LAST_DUR, len(re.findall(r"\w+", txt)) / DEFAULT_WPS)
        cues.append(Cue(t0, t0 + dur, txt))
        t0 += dur
    return cues

# ================== Indexación local ==================
def preload_local_media():
    """Escanea TODAS las carpetas dentro de data/ y construye MEDIA_DB."""
    MEDIA_DB.clear()
    if not os.path.isdir(DATA_DIR):
        return
    roots = [d for d in os.listdir(DATA_DIR) if os.path.isdir(os.path.join(DATA_DIR, d))]
    for root in roots:
        root_path = os.path.join(DATA_DIR, root)
        candidates: Dict[str, Dict[str, str]] = {}
        for dirpath, _, files in os.walk(root_path):
            rel_dir = os.path.relpath(dirpath, root_path)
            if rel_dir == ".":
                rel_dir = ""
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                base = os.path.splitext(f)[0]
                if ext not in AUDIO_EXTS + TEXT_EXTS:
                    continue
                rel_base = base if not rel_dir else f"{rel_dir.replace(os.sep, '/')}/{base}"
                key = f"{root}/{rel_base}"
                entry = candidates.setdefault(key, {})
                full = os.path.join(dirpath, f)
                if ext in AUDIO_EXTS:
                    entry["audio"] = full
                else:
                    entry["subs"] = full
        for key, parts in candidates.items():
            if "audio" not in parts or "subs" not in parts:
                continue
            try:
                with open(parts["subs"], encoding="utf-8", errors="ignore") as f:
                    raw = f.read()
                cues = parse_srt_vtt(raw) if parts["subs"].lower().endswith((".srt", ".vtt")) else parse_txt(raw)
                MEDIA_DB[key] = {"audio": parts["audio"], "cues": cues}
            except Exception as e:
                print(f"[preload] error {key}: {e}")

# ================== Helpers de nombre y paginación ==================
def _clean_material_name(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\s*\(\s*\d+\s+l[ií]neas\s*\)\s*$", "", s, flags=re.IGNORECASE)
    return s.strip(" '\"“”‘’")

def _resolve_key(name: str) -> Optional[str]:
    name = _clean_material_name(name)
    if name in MEDIA_DB:
        return name
    lname = name.lower()
    for k in MEDIA_DB.keys():
        if k.lower() == lname:
            return k
    candidates = [k for k in MEDIA_DB.keys() if k.lower().endswith("/" + lname) or os.path.basename(k).lower() == lname]
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        candidates.sort(key=len)
        return candidates[-1]
    return None

def parse_cmd_with_page(text: str) -> Tuple[str, int]:
    """Devuelve (query, page). /list [page]  |  /search <query> [page]"""
    parts = text.strip().split(maxsplit=2)
    if len(parts) == 1:
        return "", 1
    if len(parts) == 2:
        return ("", int(parts[1])) if parts[1].isdigit() else (parts[1], 1)
    # len == 3
    q, maybe_page = parts[1], parts[2]
    if maybe_page.isdigit():
        return q, int(maybe_page)
    return f"{parts[1]} {parts[2]}", 1

# ---- Ordenación natural y bloques 1–10, 11–20, … ----
def natsort_key(path: str):
    """
    Clave de ordenación natural (numérica) por todo el path.
    Evita que '10' vaya antes que '110'.
    """
    tokens: List[object] = []
    for seg in path.split('/'):
        for part in re.split(r'(\d+)', seg.lower()):
            tokens.append(int(part) if part.isdigit() else part)
    return tokens

def extract_last_number(key: str) -> Optional[int]:
    """
    Toma el ÚLTIMO número del basename (por ejemplo, Track_110 -> 110).
    Devuelve None si no hay número.
    """
    nums = re.findall(r'\d+', os.path.basename(key))
    return int(nums[-1]) if nums else None

def range_label_from_n(n: int) -> str:
    """Devuelve la etiqueta de bloque por decenas: 1–10, 11–20, etc., según n."""
    start = ((n - 1) // 10) * 10 + 1
    end = start + 9
    return f"{start}–{end}"

def build_page(keys: List[str], page: int, title: str) -> str:
    """
    Construye el texto de una página:
    - Orden natural (10 no va antes que 110)
    - Cabeceras por bloques 1–10, 11–20, etc. según el último número del basename
    - Respeta el límite (~4096) usando MSG_BUDGET
    """
    if not keys:
        return f"{title} (vacío)"

    # 1) Ordenación natural
    keys_sorted = sorted(keys, key=natsort_key)

    # 2) Paginación
    total = len(keys_sorted)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)
    slice_keys = keys_sorted[start:end]

    header = f"{title} (pág. {page}/{total_pages}, total {total}):\n"
    out = header
    used = len(out)

    last_bucket: Optional[object] = None  # bucket numérico o la cadena "otros"
    for k in slice_keys:
        n = extract_last_number(k)
        if n is not None:
            bucket = (n - 1) // 10  # 0->1–10, 1->11–20, ...
            if bucket != last_bucket:
                # Nueva cabecera de bloque
                block_label = range_label_from_n(n)
                block_header = f"\n[{block_label}]\n"
                if used + len(block_header) > MSG_BUDGET:
                    break
                out += block_header
                used += len(block_header)
                last_bucket = bucket
        else:
            if last_bucket != "otros":
                block_header = "\n[Otros]\n"
                if used + len(block_header) > MSG_BUDGET:
                    break
                out += block_header
                used += len(block_header)
                last_bucket = "otros"

        line = f"• {k} ({len(MEDIA_DB[k].get('cues') or [])} líneas)\n"
        if used + len(line) > MSG_BUDGET:
            break
        out += line
        used += len(line)

    if used == len(header):
        out += "(sin elementos en esta página)"

    return out.rstrip()

# ================== Aiogram ==================
dp = Dispatcher()

@dp.message(Command("start"))
async def start_cmd(msg: Message):
    await msg.answer(
        "Hola 👋\n"
        "• /list [página] → ver materiales (paginado)\n"
        "• /search <texto> [página] → filtrar por nombre\n"
        "• /rescan → reindexar data/\n"
        "• /play <clave|nombre> → audio + texto con traducción debajo"
    )

@dp.message(Command("list")))
async def list_cmd(msg: Message):
    preload_local_media()
    if not MEDIA_DB:
        await msg.answer("No he encontrado materiales en data/.")
        return
    _, page = parse_cmd_with_page(msg.text or "/list")
    keys = list(MEDIA_DB.keys())  # NO ordenar aquí; lo hace build_page (natural)
    text = build_page(keys, page, "Materiales encontrados")
    await msg.answer(text)

@dp.message(Command("search")))
async def search_cmd(msg: Message):
    preload_local_media()
    query, page = parse_cmd_with_page(msg.text or "/search")
    q = query.strip().lower()
    if not q:
        await msg.answer("Uso: /search <texto> [página]")
        return
    keys = [k for k in MEDIA_DB.keys() if q in k.lower()]  # sin ordenar aquí
    if not keys:
        await msg.answer("Sin resultados.")
        return
    text = build_page(keys, page, f"Resultados para “{query}”")
    await msg.answer(text)

@dp.message(Command("rescan")))
async def rescan_cmd(msg: Message):
    preload_local_media()
    await msg.answer(f"Reindexado. Total materiales: {len(MEDIA_DB)}")

@dp.message(Command("play")))
async def play_cmd(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer("Uso: /play <clave o nombre>")
        return
    raw = parts[1]
    key = _resolve_key(raw)
    if not key or key not in MEDIA_DB:
        await msg.answer("No encuentro ese material. Prueba /list o /search.")
        return

    item = MEDIA_DB[key]
    audio_path: Optional[str] = item.get("audio")  # type: ignore
    cues: List[Cue] = item.get("cues")  # type: ignore
    if not audio_path or not cues:
        await msg.answer("Faltan archivos para ese material.")
        return

    # 1) Enviar audio
    try:
        await msg.answer_audio(audio=FSInputFile(audio_path), caption=f"▶ {key}")
    except Exception as e:
        await msg.answer(f"No pude enviar el audio: {e}")
        return

    # 2) Enviar texto original + traducción debajo (sin fonética)
    out_lines: List[str] = []
    for c in cues:
        orig = c.text
        trans = translate_line(orig)
        out_lines.append(f"{orig}\n{trans}\n")

    full_text = "\n".join(out_lines).strip()

    # fragmentar para no superar límites de Telegram
    maxlen = 3500
    for i in range(0, len(full_text), maxlen):
        await msg.answer(full_text[i:i+maxlen])

# ================== Main ==================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Falta TELEGRAM_TOKEN en el entorno.")
    preload_local_media()
    bot = Bot(BOT_TOKEN, parse_mode=None)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
