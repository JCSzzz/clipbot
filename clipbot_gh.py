#!/usr/bin/env python3
"""ClipBot (GitHub Actions): tema -> vídeo -> cortes narrados -> YouTube agendado.

Roda uma vez por execução do workflow:
  1. escolhe um tema (rodízio) e busca um vídeo novo no YouTube (ou usa o link informado)
  2. pega as legendas do vídeo e pede à Groq os melhores trechos de 2~3 min
  3. baixa só esses trechos, escreve a narração, gera a voz e edita em vertical
  4. envia para o YouTube já agendado (ou privado, no modo teste)
"""
import asyncio
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
SRC, OUT = ROOT / "work", ROOT / "out"
STATE_F, CFG_F = ROOT / "state.json", ROOT / "themes.yml"
SRC.mkdir(exist_ok=True)
OUT.mkdir(exist_ok=True)

CFG = yaml.safe_load(CFG_F.read_text(encoding="utf-8"))
SET = CFG.get("config", {})
MODE = SET.get("modo", "teste")  # teste = privado, sem agendar | producao = agenda e publica
TIMES = SET.get("horarios", ["12:00", "18:00", "21:00"])
TZ = dt.timezone(dt.timedelta(hours=SET.get("fuso_horas", -3)))
VOICE = SET.get("voz", "pt-BR-AntonioNeural")
LLM_MODEL = SET.get("modelo_llm", "auto")
WORDS_PER_SEC = 2.6
MIN_DUR, MAX_DUR = 90, 200
ISO = "%Y-%m-%dT%H:%M:%SZ"


# ---------- utilidades ----------
def run(cmd, **kw):
    subprocess.run(cmd, check=True, **kw)


def duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)])
    return float(out)


_GROQ_CLIENT = None
_GROQ_MODEL = None


def groq_model():
    """Seleciona um modelo de chat ativo e acessível pela chave cadastrada."""
    global _GROQ_CLIENT, _GROQ_MODEL
    if _GROQ_MODEL:
        return _GROQ_CLIENT, _GROQ_MODEL

    from groq import Groq
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY não configurada nos Secrets do GitHub.")

    _GROQ_CLIENT = Groq(api_key=api_key)
    available = {
        model.id for model in _GROQ_CLIENT.models.list().data
        if getattr(model, "active", True)
    }
    preferred = ([LLM_MODEL] if LLM_MODEL and LLM_MODEL.lower() != "auto" else []) + [
        "openai/gpt-oss-120b",
        "openai/gpt-oss-20b",
        "qwen/qwen3.8-27b",
    ]
    for model_id in preferred:
        if model_id in available:
            _GROQ_MODEL = model_id
            print(f"Modelo Groq selecionado: {_GROQ_MODEL}")
            return _GROQ_CLIENT, _GROQ_MODEL

    excluded = ("whisper", "guard", "embed", "speech", "transcribe", "tts", "audio")
    candidates = sorted(
        model_id for model_id in available
        if not any(term in model_id.lower() for term in excluded)
    )
    if not candidates:
        raise RuntimeError("A chave Groq não possui um modelo de chat ativo disponível.")
    _GROQ_MODEL = candidates[0]
    print(f"Modelo Groq selecionado automaticamente: {_GROQ_MODEL}")
    return _GROQ_CLIENT, _GROQ_MODEL


def llm(system, prompt, max_tokens=4000):
    client, model = groq_model()
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError(f"A Groq retornou uma resposta vazia (modelo {model}).")
    return content.strip()


def load_state():
    if STATE_F.exists():
        return json.loads(STATE_F.read_text())
    return {"seen": [], "rr": 0, "slots": []}


def save_state(state):
    STATE_F.write_text(json.dumps(state, ensure_ascii=False, indent=1))


def next_slot(state):
    """Próximo horário livre (UTC, ISO) entre os horários configurados."""
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
    used = {s for s in state.get("slots", []) if s > now.strftime(ISO)}
    day = now.astimezone(TZ).date()
    for _ in range(400):
        for tm in sorted(TIMES):
            h, m = map(int, tm.split(":"))
            cand = dt.datetime.combine(day, dt.time(h, m), tzinfo=TZ).astimezone(dt.timezone.utc)
            iso = cand.strftime(ISO)
            if cand > now and iso not in used:
                state["slots"] = sorted(used | {iso})
                return iso
        day += dt.timedelta(days=1)


# ---------- busca ----------
def search(theme, seen):
    from googleapiclient.discovery import build
    yt = build("youtube", "v3", developerKey=os.environ["YT_API_KEY"])
    p = dict(part="snippet", q=theme["busca"], type="video", maxResults=20,
             videoDuration=theme.get("duracao", "long"),
             relevanceLanguage=theme.get("idioma", "pt"))
    if theme.get("so_creative_commons", True):
        p["videoLicense"] = "creativeCommon"
    items = yt.search().list(**p).execute().get("items", [])
    return [i["id"]["videoId"] for i in items if i["id"]["videoId"] not in seen]


# ---------- yt-dlp ----------
def ydl_opts(**extra):
    o = {"quiet": True, "no_warnings": True, "noprogress": True}
    ck = ROOT / "cookies.txt"
    if ck.exists() and ck.stat().st_size > 0:
        o["cookiefile"] = str(ck)
    o.update(extra)
    return o


def parse_json3(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    segs, last = [], ""
    for ev in data.get("events", []):
        if "segs" not in ev:
            continue
        text = "".join(s.get("utf8", "") for s in ev["segs"]).replace("\n", " ").strip()
        if not text or text == last:
            continue
        last = text
        start = ev.get("tStartMs", 0) / 1000
        segs.append((start, start + ev.get("dDurationMs", 2000) / 1000, text))
    return segs


def fetch_captions(url, lang):
    import yt_dlp
    opts = ydl_opts(skip_download=True, writesubtitles=True, writeautomaticsub=True,
                    subtitleslangs=[f"{lang}.*"], subtitlesformat="json3",
                    outtmpl=str(SRC / "%(id)s"))
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    files = sorted(glob.glob(str(SRC / f"{info['id']}.*.json3")), key=lambda f: "-orig" not in f)
    return info, (parse_json3(files[0]) if files else [])


def download_clip(url, vid, i, start, end):
    import yt_dlp
    from yt_dlp.utils import download_range_func
    opts = ydl_opts(format="bv*[height<=720]+ba/b[height<=720]/b", merge_output_format="mp4",
                    outtmpl=str(SRC / f"{vid}_{i}.%(ext)s"),
                    download_ranges=download_range_func(None, [(start, end)]),
                    force_keyframes_at_cuts=True)
    with yt_dlp.YoutubeDL(opts) as y:
        y.download([url])
    return SRC / f"{vid}_{i}.mp4"


# ---------- LLM ----------
def pick_segments(segs, n, title, total):
    transcript = "\n".join(f"[{int(s)}] {t}" for s, _, t in segs)
    txt = llm(
        "Você é editor de vídeos curtos virais. Responda APENAS com JSON válido.",
        f"Vídeo: {title}\nDuração total: {int(total)}s\n\nLegenda com tempo em segundos:\n{transcript}\n\n"
        f"Escolha até {n} trechos NÃO sobrepostos, cada um com 120 a 180 segundos, que façam sentido "
        "sozinhos, começando e terminando em fim de frase e com gancho forte. "
        'Formato: [{"start": 12, "end": 150, "title": "título curto e chamativo em português"}]')
    picks = json.loads(re.search(r"\[.*\]", txt, re.S).group(0))
    ok = [p for p in picks if MIN_DUR <= p["end"] - p["start"] <= MAX_DUR
          and 0 <= p["start"] < p["end"] <= total + 1]
    return ok[:n]


def write_narration(clip_text, dur, lang, title):
    words = int(dur * WORDS_PER_SEC)
    return llm(
        "Você escreve narrações para vídeos curtos em português do Brasil. "
        "Responda somente com o texto falado, sem emojis, sem marcações, sem indicações de cena.",
        f"Título: {title}\nIdioma original: {lang}\n\nTrecho do vídeo (legenda):\n{clip_text}\n\n"
        f"Escreva uma narração de aproximadamente {words} palavras que explique e comente esse trecho. "
        "Comece com um gancho nos primeiros 3 segundos, use frases curtas, traga contexto e uma "
        "observação própria, e feche com uma frase que prenda o espectador. "
        "Não invente fatos que não estejam no trecho.")


def tts(text, out_mp3):
    import edge_tts
    asyncio.run(edge_tts.Communicate(text, VOICE).save(str(out_mp3)))


# ---------- edição ----------
def srt_time(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02}:{int(m):02}:{int(s):02},{int((s % 1) * 1000):03}"


def make_srt(text, total, srt_path, group=5):
    """Legendas de ~5 palavras, com o tempo distribuído pelo tamanho do texto."""
    words = text.split()
    chunks = [words[i:i + group] for i in range(0, len(words), group)]
    weights = [sum(len(w) + 1 for w in c) for c in chunks]
    tot, t, lines = sum(weights), 0.0, []
    for n, (c, w) in enumerate(zip(chunks, weights), 1):
        d = total * w / tot
        lines.append(f"{n}\n{srt_time(t)} --> {srt_time(t + d)}\n{' '.join(c)}\n")
        t += d
    srt_path.write_text("\n".join(lines), encoding="utf-8")


def render(clip, narr_text, narr_mp3, out_mp4):
    clip_d, narr_d = duration(clip), duration(narr_mp3)
    f = min(max(narr_d / clip_d, 0.8), 1.25)  # estica/encurta o vídeo para casar com a voz
    srt = out_mp4.with_suffix(".srt")
    make_srt(narr_text, narr_d, srt)
    fc = (
        "[0:v]split[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];"
        "[b]scale=1080:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setpts=PTS*{f:.4f},"
        "tpad=stop_mode=clone:stop_duration=30,"
        f"subtitles={srt.name}:force_style='Fontsize=11,Bold=1,Outline=2,Alignment=2,MarginV=170'[v];"
        f"[0:a]volume=0.10,atempo={1 / f:.4f}[o];[1:a]volume=1.0[n];"
        "[o][n]amix=inputs=2:duration=longest:dropout_transition=0,volume=2[a]"
    )
    run(["ffmpeg", "-y", "-i", str(Path(clip).resolve()), "-i", str(Path(narr_mp3).resolve()),
         "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-t", f"{narr_d:.2f}",
         "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-c:a", "aac", "-b:a", "160k",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_mp4.name], cwd=out_mp4.parent)


# ---------- YouTube ----------
def yt_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    info = json.loads(os.environ["YT_TOKEN_JSON"])
    creds = Credentials.from_authorized_user_info(
        info, ["https://www.googleapis.com/auth/youtube.upload"])
    if not creds.valid:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def upload(yt, path, title, description, publish_at):
    from googleapiclient.http import MediaFileUpload
    status = {"privacyStatus": "private", "selfDeclaredMadeForKids": False,
              "containsSyntheticMedia": True}  # confira o campo na doc atual da API
    if MODE == "producao":
        status["publishAt"] = publish_at
    body = {"snippet": {"title": title[:100], "description": description, "categoryId": "22"},
            "status": status}
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(path), resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    print(f"YouTube ok: {resp['id']} ({'agendado ' + publish_at if MODE == 'producao' else 'privado'})")


# ---------- fluxo ----------
def process_video(url, vid, theme, state, yt, manual):
    lang = theme.get("idioma", "pt")
    info, segs = fetch_captions(url, lang)
    total = info.get("duration") or 0
    if not segs:
        print("sem legendas, pulando")
        return False
    if total > 3 * 3600:
        print("vídeo longo demais, pulando")
        return False
    picks = pick_segments(segs, int(theme.get("cortes", 3)), info.get("title", ""), total)
    print(f"{len(picks)} cortes escolhidos em '{info.get('title')}'")
    made = 0
    for i, p in enumerate(picks):
        try:
            clip = download_clip(url, vid, i, p["start"], p["end"])
            text = " ".join(t for s, e, t in segs if s >= p["start"] - 1 and e <= p["end"] + 1)
            narr = write_narration(text, p["end"] - p["start"], lang, p["title"])
            mp3, mp4 = OUT / f"{vid}_{i}.mp3", OUT / f"{vid}_{i}.mp4"
            tts(narr, mp3)
            render(clip, narr, mp3, mp4)
            lic = "" if manual or not theme.get("so_creative_commons", True) else \
                " (licença Creative Commons BY)"
            desc = (f"{p['title']}\n\nVídeo original: {info.get('title')} — "
                    f"{info.get('uploader', '')}{lic}\n{url}\nNarração gerada por IA.\n\n#Shorts")
            (OUT / f"{vid}_{i}.txt").write_text(f"{p['title']}\n\n{desc}", encoding="utf-8")
            if yt:
                upload(yt, mp4, p["title"], desc, next_slot(state))
            made += 1
        except Exception as e:  # noqa: BLE001
            print(f"corte {i} falhou: {e}")
        finally:
            save_state(state)
    return made > 0


def main():
    state = load_state()
    manual = os.getenv("INPUT_URL", "").strip()
    jobs = []
    if manual:
        jobs.append((manual, {"idioma": os.getenv("INPUT_LANG") or "pt",
                              "cortes": int(os.getenv("INPUT_CLIPS") or 3)}))
    else:
        themes = [t for t in CFG.get("temas", []) if t.get("ativo", True)]
        if not themes:
            sys.exit("Nenhum tema ativo em themes.yml")
        for k in range(len(themes)):
            t = themes[(state["rr"] + k) % len(themes)]
            ids = search(t, set(state["seen"]))[:6]
            if ids:
                print(f"Tema: {t['nome']}")
                jobs = [(f"https://www.youtube.com/watch?v={v}", t) for v in ids]
                state["rr"] = (state["rr"] + k + 1) % len(themes)
                break
    yt = yt_service() if os.getenv("YT_TOKEN_JSON") else None
    if not yt:
        print("YT_TOKEN_JSON ausente: só gera os arquivos (baixe em 'Artifacts').")
    for url, theme in jobs:
        m = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", url)
        vid = m.group(1) if m else "video"
        if vid not in state["seen"]:
            state["seen"].append(vid)
        save_state(state)
        try:
            if process_video(url, vid, theme, state, yt, bool(manual)):
                return
        except Exception as e:  # noqa: BLE001
            print(f"falhou em {url}: {e}")
    save_state(state)
    sys.exit("Nenhum vídeo foi processado com sucesso nesta execução.")


if __name__ == "__main__":
    main()

