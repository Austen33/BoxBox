from telegram import Update
from telegram.ext import ContextTypes
from utils.f1_data import get_full_race_results_async
from utils.groq_client import chat, FAST_MODEL
from utils.tavily_client import search
from utils.rate_limit import is_rate_limited
from utils.telegram_safe import safe_reply


def _is_dnf(status: str) -> bool:
    finished = {"Finished"}
    if status in finished:
        return False
    if status.startswith("+") and "Lap" in status:
        return False
    return bool(status)


async def result_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if is_rate_limited(user_id):
        await update.message.reply_text("Slow down — one question at a time.")
        return

    await update.message.reply_chat_action("typing")

    data = await get_full_race_results_async()
    if not data or "error" in data:
        err = data.get("error", "unknown") if data else "unknown"
        await update.message.reply_text(f"Couldn't fetch race results: {err}")
        return

    race_name = data["name"]
    year = data["year"]
    results = data["results"]

    finishers = [r for r in results if not _is_dnf(r["status"])]
    dnfs = [r for r in results if _is_dnf(r["status"])]

    dnf_reasons: dict[str, str] = {}
    if dnfs:
        dnf_names = ", ".join(r["driver"] for r in dnfs)
        search_results = await search(
            f"F1 {race_name} {year} DNF retirement crash incident {dnf_names}",
            max_results=5,
        )
        news_context = ""
        if search_results:
            for item in search_results[:4]:
                news_context += f"{item.get('title', '')}: {item.get('content', '')[:250]}\n"

        dnf_list = "\n".join(
            f"- {r['driver']} ({r['team']}): API status = \"{r['status']}\""
            for r in dnfs
        )
        prompt = (
            f"Race: {race_name} {year}\n\n"
            f"These drivers retired (DNF):\n{dnf_list}\n\n"
            f"News context:\n{news_context}\n\n"
            "For each DNF driver write one short phrase (3-6 words max) explaining why they retired. "
            "Examples: 'collision with Norris', 'engine failure', 'brake failure lap 44', 'hydraulics'. "
            "If you can name the other car involved in a collision, do so. "
            "Output ONLY lines in the format: DriverLastName: reason"
        )
        raw = await chat(
            messages=[{"role": "user", "content": prompt}],
            model=FAST_MODEL,
        )
        for line in raw.strip().splitlines():
            if ":" in line:
                name_part, reason = line.split(":", 1)
                name_part = name_part.strip().lower()
                for r in dnfs:
                    last = r["driver"].split()[-1].lower()
                    full_lower = r["driver"].lower()
                    if name_part in (last, full_lower) or last in name_part:
                        dnf_reasons[r["driver"]] = reason.strip()
                        break

    lines: list[str] = [f"*{race_name} {year}*\n"]

    for r in finishers:
        pos = r["position"]
        name = r["driver"]
        team = r["team"]
        gap = r["gap"]
        if pos == 1:
            lines.append(f"P1  {name} ({team})")
        else:
            suffix = f" +{gap}" if gap else f" {r['status']}"
            lines.append(f"P{pos:<2} {name} ({team}){suffix}")

    if dnfs:
        lines.append("")
        lines.append("DNF:")
        for r in dnfs:
            reason = dnf_reasons.get(r["driver"], r["status"].lower())
            last = r["driver"].split()[-1]
            lines.append(f"{last} — {reason}")

    await safe_reply(update.message, "\n".join(lines))
