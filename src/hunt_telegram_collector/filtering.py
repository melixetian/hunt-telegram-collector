import re

DEFAULTS = {
    "vacancy_patterns": [
        r"ваканси",
        r"ищем",
        r"требуется",
        r"нанима",
        r"hiring",
        r"vacanc",
        r"job opening",
        r"join our team",
    ],
    "relevance_patterns": [
        r"python",
        r"back.?end",
        r"б[эе]кенд",
        r"golang",
        r"\bgo\b",
        r"\bapi\b",
        r"robot",
        r"робот",
        r"\b[wr]ms\b",
        r"physical ai",
        r"distributed",
        r"распредел",
        r"\bai\b",
        r"machine learning",
    ],
    "resume_patterns": [
        r"ищу работу",
        r"ищу проект",
        r"ищу ваканс",
        r"#резюме",
        r"#resume",
        r"looking for (a |new )?(job|role|opportunit)",
        r"open to work",
    ],
}


class Filter:
    def __init__(self, overrides=None):
        self.patterns = {
            name: [re.compile(p, re.IGNORECASE) for p in patterns]
            for name, patterns in (DEFAULTS | (overrides or {})).items()
        }

    def classify(self, text, has_media, kind):
        if not text.strip():
            return ("review", "media_without_text") if has_media else ("noise", "empty_or_service")
        found = {
            name: any(p.search(text) for p in patterns) for name, patterns in self.patterns.items()
        }
        if found["resume_patterns"]:
            reason = (
                "ambiguous_resume_and_vacancy" if found["vacancy_patterns"] else "likely_resume"
            )
            return "review", reason
        if kind == "jobs":
            return "candidate", "jobs_source_broad_coverage"
        if found["vacancy_patterns"] and found["relevance_patterns"]:
            return "candidate", "vacancy_and_professional_signal"
        if found["vacancy_patterns"] or found["relevance_patterns"]:
            return "review", "partial_signal"
        return "noise", "no_configured_signals"
