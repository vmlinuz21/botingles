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

# Estimación para duraciones si el texto es .txt (sin tiempos)
DEFAULT_WPS = 2.5
MIN_LAST_DUR = 1.2

AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".ogg", ".oga", ".aac", ".flac")
TEXT_EXTS = (".txt", ".srt", ".vtt")

PAGE_SIZE = 100   # elementos por página
MSG_BUDGET = 3900 # margen para no pasar el límite de 4096 de Telegram

# ================== Traducción ==================
from deep_translator import GoogleTranslator

def translate_line(text: str) -> str:
    """Traduce una línea (auto -> es). Si falla, deja el original."""
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
        r"(\d{2}:\d{2}:\d{2}[\.,]\d{3}|\d{2}:\d{2}[\.,]\d{3})\s*-->\s*"
        r"(\d{2}:\d{2}:\d{2}[\.,]\d{3}|\d{2}:\d{2}[\.,]\d{3})"
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
    return sorted
