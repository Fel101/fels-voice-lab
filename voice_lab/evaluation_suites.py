"""Versioned, named evaluation suites. Feature showcases stay distinct from fair comparisons."""

SUITE_SCHEMA_VERSION = 1


def _case(case_id, text, *, direction="", language="English", scope="common_capability", note=""):
    return {"case_id": case_id, "text": text, "direction": direction, "language": language,
            "scope": scope, "note": note}


SUITES = [
    {
        "suite_id": "neutral-clone-three-line",
        "version": 1,
        "name": "Neutral identity — shared reference",
        "scope": "common_capability",
        "purpose": "Compare reference-conditioned identity on identical neutral English text.",
        "reference_required": True,
        "repeat_count": 3,
        "cases": [
            _case("neutral-01", "The train arrives at the station just before noon."),
            _case("neutral-02", "I left the blue folder beside the open window."),
            _case("neutral-03", "We can finish the repairs before the rain arrives."),
        ],
    },
    {
        "suite_id": "four-emotions-showcase",
        "version": 1,
        "name": "Four emotions — native direction showcase",
        "scope": "native_feature_demo",
        "purpose": "Compare each engine's actual acting controls; instructions are not assumed equivalent.",
        "reference_required": True,
        "repeat_count": 3,
        "cases": [
            _case("emotion-happy", "I can't believe you actually came back.", direction="overjoyed and excited, almost laughing", scope="native_feature_demo"),
            _case("emotion-sad", "I can't believe you actually came back.", direction="sad and tearful, voice trembling", scope="native_feature_demo"),
            _case("emotion-angry", "I can't believe you actually came back.", direction="annoyed and angry, sharp and cold", scope="native_feature_demo"),
            _case("emotion-tender", "I can't believe you actually came back.", direction="soft, relieved, and affectionate", scope="native_feature_demo"),
        ],
    },
    {
        "suite_id": "whisper-shout-showcase",
        "version": 1,
        "name": "Whisper and shout — native direction showcase",
        "scope": "native_feature_demo",
        "purpose": "Record engine-specific whisper/shout behavior without treating controls as interchangeable.",
        "reference_required": True,
        "repeat_count": 3,
        "cases": [
            _case("whisper", "Stay close. I don't want anyone else to hear this.", direction="whispering softly and intimately", scope="native_feature_demo"),
            _case("shout", "Stay close. I don't want anyone else to hear this.", direction="shouting urgently across a noisy room", scope="native_feature_demo"),
        ],
    },
    {
        "suite_id": "native-events-showcase",
        "version": 1,
        "name": "Native events — engine syntax showcase",
        "scope": "native_feature_demo",
        "purpose": "Keep event markup model-specific and preserve the exact text sent to each engine.",
        "reference_required": True,
        "repeat_count": 3,
        "cases": [
            _case("event-laugh", "(laugh) That is the worst joke I have ever heard.", scope="native_feature_demo", note="Breeze's parenthesized vocal event syntax; unsupported engines must be marked unsupported."),
            _case("event-turbo-laugh", "[laugh] That is the worst joke I have ever heard.", scope="native_feature_demo", note="Chatterbox Turbo's bracketed event syntax; do not compare markup as if common."),
        ],
    },
    {
        "suite_id": "short-barks-common",
        "version": 1,
        "name": "Short barks — shared text",
        "scope": "common_capability",
        "purpose": "Check brief reusable character lines and identity carry-through.",
        "reference_required": True,
        "repeat_count": 3,
        "cases": [_case("bark-wait", "Wait!"), _case("bark-ready", "I'm ready."), _case("bark-thanks", "Thank you.")],
    },
    {
        "suite_id": "names-numbers-common",
        "version": 1,
        "name": "Names and numbers — shared text",
        "scope": "common_capability",
        "purpose": "Check names, dates, times, and spoken number rendering.",
        "reference_required": True,
        "repeat_count": 3,
        "cases": [_case("names-numbers-01", "Meet Dr. Amina Vale at 7:45 a.m. on October 21, 2031.")],
    },
    {
        "suite_id": "pronunciation-traps-common",
        "version": 1,
        "name": "Pronunciation traps — shared text",
        "scope": "common_capability",
        "purpose": "Expose proper-name and homograph differences for later transcript-aided evaluation.",
        "reference_required": True,
        "repeat_count": 3,
        "cases": [_case("pronunciation-01", "Read the record, then read it aloud to Dr. Siobhan O'Rourke.")],
    },
    {
        "suite_id": "multilingual-lines-common",
        "version": 2,
        "name": "Multilingual lines — one sentence per language",
        "scope": "common_capability",
        "purpose": "Screen each engine's claimed languages with one sentence. A speech recognizer reads each clip back with the language forced; nobody listens, and no native speaker has checked these translations. The sentences contain no numerals, because a recognizer writes numbers as digits and each digit would count as a word error. docs/LANGUAGE_MATRIX.md records the results.",
        "reference_required": True,
        "repeat_count": 3,
        "cases": [
            _case("multilingual-english", "The next train is about to leave the station.", language="English"),
            _case("multilingual-british-english", "The next train is about to leave the station.", language="British English"),
            _case("multilingual-spanish", "El próximo tren está a punto de salir de la estación.", language="Spanish"),
            _case("multilingual-french", "Le prochain train est sur le point de quitter la gare.", language="French"),
            _case("multilingual-german", "Der nächste Zug fährt gleich aus dem Bahnhof ab.", language="German"),
            _case("multilingual-italian", "Il prossimo treno sta per lasciare la stazione.", language="Italian"),
            _case("multilingual-portuguese", "O próximo trem está prestes a sair da estação.", language="Portuguese"),
            _case("multilingual-russian", "Следующий поезд скоро отправится от этой станции.", language="Russian"),
            _case("multilingual-chinese", "下一班火车马上就要离开车站了。", language="Chinese"),
            _case("multilingual-japanese", "次の電車はまもなく駅を出発します。", language="Japanese"),
            _case("multilingual-korean", "다음 열차가 곧 이 역을 떠나려고 합니다.", language="Korean"),
            _case("multilingual-hindi", "अगली ट्रेन जल्दी ही स्टेशन से निकलने वाली है।", language="Hindi"),
            _case("multilingual-arabic", "القطار التالي على وشك مغادرة المحطة.", language="Arabic"),
        ],
    },
    {
        "suite_id": "paragraph-continuity-common",
        "version": 1,
        "name": "Paragraph continuity — shared reference",
        "scope": "common_capability",
        "purpose": "Inspect identity and delivery drift across multiple calls in one short passage.",
        "reference_required": True,
        "repeat_count": 3,
        "cases": [
            _case("continuity-01", "The room was quiet when I arrived. I set the letter on the table and waited. By the time the kettle began to sing, I knew what I wanted to say."),
            _case("continuity-02", "Outside, the rain softened to a whisper. The streetlights came on one by one, and the whole city seemed to take a slower breath."),
        ],
    },
    {
        "suite_id": "scene-consistency-common",
        "version": 1,
        "name": "Scene consistency — shared cast and direction",
        "scope": "common_capability",
        "purpose": "Inspect voice identity and acting continuity in repeatable scene lines; synthesis remains sequential.",
        "reference_required": True,
        "repeat_count": 3,
        "cases": [
            _case("scene-01", "The gate is still open."),
            _case("scene-02", "I thought you said the road was clear."),
            _case("scene-03", "It was, until the fog rolled in."),
        ],
    },
]


SUITES.append({
    'suite_id':'drama3-acting-context','version':1,'name':'Drama 3 — inline acting and identity',
    'scope':'native_feature_demo','reference_required':True,'repeat_count':2,
    'supported_engines':['drama-3'],
    'purpose':'Review spoken cues, extended events and the same speaker across turns; human identity review remains required.',
    'cases':[
        _case('drama-cue','[whispers] Stay close. [normal voice] The gate is open.',scope='native_feature_demo'),
        _case('drama-event','[sighs] Fine. We will wait here.',scope='native_feature_demo'),
        _case('drama-identity','<|speaker:0|> Keep the gate closed. <|speaker:0|> I will return at sunrise.',scope='native_feature_demo')]
})


def list_suites():
    return {"schema_version": SUITE_SCHEMA_VERSION, "suites": SUITES}


def get_suite(suite_id):
    for suite in SUITES:
        if suite["suite_id"] == suite_id:
            return suite
    raise KeyError(f"Evaluation suite {suite_id!r} was not found.")
