# Copied from artel-agent-server app/qa/pulse.py (project-artel, same org).
# The pulse wire format and how deltas fold into a view are defined there; this copy
# only replaces the `center_of` import so the module stands alone.

"""What the Agent remembers from the pulse channel.

`GAME_STATE` says what the screen is *now*, and `app/qa/scene.py` reconstructs
change by diffing one snapshot against the last. Pulse hands that over as the
original: the SDK reads watched members every 0.1s and reports the ones that
moved, so "the score rose" arrives as a fact rather than as an inference drawn
between two observations.

Two kinds of reading arrive on the same channel and `whole` tells them apart:

    whole=true    everything the reading can see. Sent on the first reading and
                  whenever the scene changes, and after a delivery is lost
    whole=false   only what moved since the last one

A delta lands on top of what is already held. **An object the reading says
nothing about keeps what it had, including which list it was in** — its
absence is not news, and had it changed the reading would have carried it.

Values are kept exactly as they arrive. What `flag == 1` means is a question
for whoever writes the spec, not for this module: interpreting it here would
put a second opinion between the game and the step that judges it.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from artel_mcp.envelope import center_of

# 한 번에 들여다볼 수 있는 객체 수. 부분 일치가 많이 걸려도 프롬프트를 삼키지 않도록.
MAX_INSPECTED = 5

# How many readings a key survives after it stops being reported.
#
# Only used for the change counter below — held objects and their values are
# never dropped, because a value that stops moving is still the value.
MAX_TRACKED_KEYS = 4096

# 판독을 도착한 순서대로 몇 개나 들고 있는지.
#
# `scene.py` 의 `MAX_VALUES_PER_OBSERVABLE` 과 같은 값이고 같은 이유다: 접힌 상태는 지금 무엇이
# 참인지만 말하고, 어떻게 거기 이르렀는지는 말하지 않는다. 스텝의 `expected` 는 거의가 변화에
# 대한 진술이라 순서가 근거의 절반이다.
#
# 문서 전체가 아니라 한 줄씩 남긴다. 전량 판독이 실측 약 18 KB 라 열 개를 통째로 실으면 그것만
# 으로 프롬프트가 채워지고, 순서를 읽는 데 필요한 것은 몇 번째 판독이 무엇을 움직였나뿐이다.
MAX_READING_LOG = 10

# 한 판독의 로그 줄이 이름 대는 키의 상한.
#
# 전량 판독은 `changed` 에 **감시 중인 키를 전부** 담는다 — SDK 의 장부가 직전 값과 다른 것을
# 넣는데 전량 판독에는 직전이 없기 때문이다(`LiveState.Ledger.Say`). 샘플 게임 실측이
# `watching 111` 이라 그것을 한 줄에 다 쓰면 로그 한 줄이 화면을 덮고, 더 큰 게임에서는 더하다.
#
# 세는 것과 이름 대는 것을 가른다. 몇 개가 움직였나는 언제나 정확하고, 어느 것인지는 여기까지다.
MAX_CHANGED_NAMED = 8
# 화면의 글자를 한 번에 몇 줄까지 싣나.
#
# 이 절은 창(window)을 안 타고 매 턴 전부 다시 실린다. 그래서 이 값이 곧 매 턴 드는 비용이다 —
# SDK 가 글자를 200 자까지 싣고 주소가 뒤에 붙으므로, 30 줄이면 이 절의 상한이 6 KB 남짓이다.
# 실제 라벨은 그보다 훨씬 짧다.
#
# 30 으로 잡은 이유: 한 화면에 이보다 많은 글자가 떠 있는 것은 목록·상점 같은 화면이고, 그때
# 읽는 쪽에 필요한 것은 전부가 아니라 이번에 바뀐 줄과 "더 있다" 는 사실이다. 넘으면 그 둘을 준다.
MAX_SCREEN_TEXT = 30
# `gone`·`deactive` 를 한 줄에 몇 개까지 이름 대나. scene 이 헐리는 순간에는 수십이 한꺼번에
# 꺼지는데, 그때 필요한 것은 명단이 아니라 "많이 꺼졌다" 는 사실이다.
MAX_NAMED_OFF = 12

PULSE_VIEW_START = "<<pulse>>"
PULSE_VIEW_END = "<<end pulse>>"


class PulseMember(BaseModel):
    """One watched member on one object, as the reading reported it.

    `value` is deliberately `Any`. The channel carries scalars as well as the
    shapes the SDK uses for a reference (`{"path": ..., "world": ...}`), a
    sprite, an animator state, a label, or a bare count — and a reader that
    narrowed this to a scalar would drop the half that names what a step is
    pointing at.
    """

    model_config = ConfigDict(extra="allow")

    # The declaring type, as the analyser named it. Kept apart from `member`
    # because two objects can offer the same property name.
    on: str | None = None
    member: str | None = None
    # Which of several same-typed components on one object this is. The SDK
    # counts them because a GameObject may carry a behaviour twice, and without
    # it the second overwrites the first.
    among: int | None = None
    value: Any = None
    # False when the reading read it because it could, rather than because the
    # evidence named it. The distinction is the whole point of carrying it: a
    # condition written against a named member means more than one that happens
    # to be readable.
    asked: bool | None = None

    @property
    def key(self) -> str:
        return f"{self.on or ''}::{self.member or ''}#{self.among or 0}"


class PulseStatic(BaseModel):
    """A value that hangs off no GameObject.

    Kept in its own table rather than under an object, because it belongs to
    none — folding statics in beside instance values would invent an owner.
    """

    model_config = ConfigDict(extra="allow")

    declaring: str | None = None
    member: str | None = None
    type: str | None = None
    value: Any = None

    @property
    def key(self) -> str:
        return f"{self.declaring or ''}::{self.member or ''}"


class PulseObject(BaseModel):
    model_config = ConfigDict(extra="allow")

    scene: str | None = None
    # The SDK's instance id. It does not survive the process, which is why
    # `selector` rides along and why the key below prefers it.
    id: int | None = None
    path: str | None = None
    selector: str | None = None
    world: dict[str, Any] | None = None
    # 화면 좌표. 조준의 대체 수단이고, 화면 크기와 맞대면 화면 안인지도 나온다.
    rect: dict[str, Any] | None = None
    # 게임이 이 객체를 무엇으로 분류해 두었는가(ARTEL-631). `CompareTag` 로 갈라지는 규칙이
    # 흔하고, 그 갈래가 QA 가 확인해야 하는 규칙인 경우도 흔하다 — 샘플 게임에서 어느 카드가
    # 어느 조합 칸에 들어가는지가 이것으로 갈린다.
    #
    # `Untagged` 는 SDK 가 안 보낸다. 여기서 `None` 은 "태그가 없다" 이지 "모른다" 가 아니다.
    tag: str | None = None
    # 이 객체가 화면에 띄우고 있는 글자(ARTEL-678). 라벨은 멤버도 offer 도 없어서, 이 값이
    # 없으면 판독에 실려 와도 그릴 것이 없는 객체가 된다.
    #
    # SDK 가 장부에 얹어 보내므로 델타에는 **바뀐 것만** 실린다. 그래서 이 필드가 실려
    # 왔다는 사실 자체가 "이번에 바뀌었다" 이고, 아래 `text_at` 이 그것을 적는다.
    text: str | None = None
    offers: dict[str, Any] | None = None
    members: list[PulseMember] = Field(default_factory=list)
    # 컴포넌트별로 묶인 멤버. `on` 을 멤버마다 되풀이하지 않으려고 SDK 가 이렇게 낸다
    # (ARTEL-540) — 한 문서에서 `on` 316개 중 295개가 같은 값이었다.
    by: list["PulseComponent"] = Field(default_factory=list)

    def flatten(self, scene: str | None) -> "PulseObject":
        """읽는 쪽이 쓰던 모양으로 되돌린다.

        접는 것은 전송의 사정이지 이 메모리의 사정이 아니다. 여기서 한 번 펴 두면 병합도
        렌더도 종전 그대로다 — 그 둘이 접힌 모양을 알 이유가 없다.
        """
        if not self.by and self.scene is not None:
            return self

        spread = list(self.members)
        for group in self.by:
            for member in group.m:
                spread.append(member.model_copy(update={"on": group.on}))

        # 최상위와 같은 씬은 객체가 제 이름을 대지 않는다. 다른 씬의 객체만 댄다.
        return self.model_copy(update={"members": spread, "by": [], "scene": self.scene or scene})

    @property
    def key(self) -> str:
        return f"{self.scene or ''}/{self.selector or self.path or ''}"


class PulseComponent(BaseModel):
    """한 컴포넌트가 내놓은 멤버들. `on` 을 한 번만 쓴다."""

    model_config = ConfigDict(extra="allow")

    on: str | None = None
    m: list[PulseMember] = Field(default_factory=list)


class PulseReading(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: int | None = Field(default=None, alias="schema")
    reading: int | None = None
    frame: int | None = None
    scene: str | None = None
    whole: bool = False
    statics: list[PulseStatic] = Field(default_factory=list)
    active: list[PulseObject] = Field(default_factory=list)
    deactive: list[PulseObject] = Field(default_factory=list)
    # 사라진 객체들. 통 둘로는 못 하는 말이라 SDK 가 따로 낸다(ARTEL-651) — 파괴된 것은
    # `active` 에도 `deactive` 에도 안 실리고 그냥 언급이 없어진다.
    #
    # 이름은 이 메모리가 객체를 세는 키와 같은 모양(`씬/selector`)이라 옮겨 적을 것이 없다.
    #
    # 없으면 없는 대로 돈다. 이 필드를 모르는 SDK 에서는 종전처럼 전량 판독까지 잔상이 남는다.
    gone: list[str] = Field(default_factory=list)
    # Written even when empty: an empty list and a missing field are different
    # claims, and the reading means the first.
    changed: list[str] = Field(default_factory=list)
    watching: int | None = None
    unresolved: int | None = None
    # What the reading could not read. Carried so a reader can tell "nothing
    # moved" from "nothing was legible".
    unwatchable: int | None = None
    gaps: list[str] = Field(default_factory=list)


class ReadingLog(BaseModel):
    """도착한 판독 하나에 대해 남기는 한 줄."""

    model_config = ConfigDict(extra="allow")

    reading: int | None = None
    frame: int | None = None
    whole: bool = False
    # 이 판독이 움직였다고 말한 키들. 값이 아니라 이름만 — 값은 접힌 상태가 들고 있고,
    # 여기서 또 들면 같은 사실이 어긋날 자리가 둘이 된다.
    #
    # `MAX_CHANGED_NAMED` 까지만 든다. 몇 개였는지는 [moved] 가 따로 들고 있으므로 잘라도
    # "몇 개가 움직였나" 는 잃지 않는다.
    changed: list[str] = Field(default_factory=list)
    # 이 판독이 움직였다고 말한 키의 총 개수. `changed` 가 잘렸는지는 이 값과 길이로 안다.
    moved: int = 0


def _key_line(offered: Any) -> str:
    """키 하나를 사람이 고를 수 있게 쓴다.

    이름만으로는 못 고른다. Map 씬의 QA 가 화살표 다섯과 Return 을 대등하게 받고 위/아래를
    눌러 보다 전투에 진입하지 못했다 — 근거는 `Return` 이 씬을 바꾼다는 것을 알고 있었는데
    그 앎이 채널에서 버려지고 있었다(ARTEL-539).

    옛 SDK 는 문자열만 보낸다. 그때는 종전대로 이름만 쓴다.
    """
    if not isinstance(offered, dict):
        return str(offered)

    name = offered.get("key") or "?"
    does = offered.get("does")
    if not does:
        # 빈 것과 없는 것을 같이 다룬다. 어느 쪽이든 "무엇을 하는지 모른다" 이고, 그것을
        # "아무 일도 안 한다" 로 읽히게 두지 않는다.
        return f"{name} (effect unknown)"
    return f"{name} → {'; '.join(str(d) for d in does)}"


class _HeldObject(BaseModel):
    model_config = ConfigDict(extra="allow")

    scene: str | None = None
    # 액션이 대상을 지목하는 값. 접을 때 버리면 독자는 무엇이 바뀌었는지 알면서
    # 그것을 건드릴 방법이 없다 — 판독이 이것을 싣는 이유가 그것이다.
    id: int | None = None
    path: str | None = None
    selector: str | None = None
    world: dict[str, Any] | None = None
    rect: dict[str, Any] | None = None
    # 게임이 이 객체를 무엇으로 분류해 두었는가(ARTEL-631).
    tag: str | None = None
    # 이 객체가 화면에 띄우고 있는 글자(ARTEL-678).
    text: str | None = None
    offers: dict[str, Any] | None = None
    # True when the object last arrived under `active`.
    live: bool = True
    members: dict[str, PulseMember] = Field(default_factory=dict)
    # 멤버마다 그 값이 마지막으로 도착한 판독 번호. 델타만 그리려면 "언제 온 것인가"가
    # 필요한데, 값 자체는 그것을 말하지 않는다 — 같은 값이 두 번 오면 구분이 안 된다.
    #
    # 판독 번호를 쓰는 이유: 판독은 이미 델타라 도착했다는 것 자체가 움직였다는 뜻이다.
    # 도구가 언제 화면을 봤는지와는 무관하게 셀 수 있다.
    at: dict[str, int] = Field(default_factory=dict)
    # 멤버마다 그 값을 **마지막으로 그린** 판독 번호. `at` 과 짝이고 묻는 질문이 다르다 —
    # `at` 은 "언제 변했나", 이것은 "언제 말했나" 다.
    #
    # 종전에는 창(`since`) 하나가 둘을 겸했다. 창의 경계는 마지막 **행위**라(ARTEL-621),
    # 행위 전에 변한 값은 탈락하고 아무도 다시 말해 주지 않아 영원히 탈락했다. 실측으로
    # 159턴 런에서 판독이 움직였다고 이름 댄 멤버 53종 중 23종이 한 번도 값을 못 냈고,
    # 거기 `Enemy::Hp` 와 `CombineZone::spellCards` 가 있었다(ARTEL-662).
    shown: dict[str, int] = Field(default_factory=dict)
    # 글자가 마지막으로 **도착한** 판독 번호. 멤버의 `at` 과 같은 질문에 답하고, 화면의
    # 글자 절이 어느 줄에 `(changed)` 를 붙일지가 이 값이다.
    #
    # 글자가 멤버가 아니라 객체 속성이라 `at` 사전에 못 들어간다. 억지로 넣으면 `on.member`
    # 모양의 이름을 지어내야 하고, 그 이름은 어느 컴포넌트에서도 오지 않은 것이 된다.
    #
    # 짝이던 `text_shown`(마지막으로 **그린** 판독 번호)은 없앴다. 화면의 글자 절이 창을 안
    # 타고 매번 전부 그리므로 "언제 말했나" 를 물을 자리가 없다(ARTEL-690).
    text_at: int = 0


class PulseMemory(BaseModel):
    """Readings folded together, newest on top.

    Mirrors the merge the SDK's own reading viewer does (`tools/watch-readings.py`),
    which is the only other reader of this channel — two readers disagreeing about
    what a delta means would be worse than either rule being wrong.
    """

    model_config = ConfigDict(extra="allow")

    readings: int = 0
    wholes: int = 0
    scene: str | None = None
    reading: int | None = None
    frame: int | None = None
    watching: int | None = None
    unresolved: int | None = None
    unwatchable: int | None = None
    statics: dict[str, PulseStatic] = Field(default_factory=dict)
    held: dict[str, _HeldObject] = Field(default_factory=dict)
    # How many readings each key moved in. This is the "it moved several times"
    # signal: a step that expects one change reads differently when the value
    # bounced five times and landed back.
    moves: dict[str, int] = Field(default_factory=dict)
    # 도착한 순서대로, 오래된 것이 앞. 상한을 넘으면 앞에서 버린다(FIFO).
    log: list[ReadingLog] = Field(default_factory=list)
    # 상한에 걸려 버린 판독이 있었나. 읽는 쪽이 "이것이 전부"라고 잘못 읽지 않도록.
    trimmed: bool = False
    # statics 도 객체 멤버와 같은 이유로 도착 시점을 남긴다.
    static_at: dict[str, int] = Field(default_factory=dict)
    # 사라진 객체와 그것이 사라진 판독. 지운 것으로 끝내지 않고 **말하기 위해** 남긴다.
    #
    # 없음은 추론이고 사라짐은 진술이다. 델타에서 안 적힌 것은 "이번에 소식이 없다" 와 구분이
    # 안 되는데, 도구 결과는 대화에 그대로 남으므로 읽는 쪽은 옛 화면에서 그 객체를 여전히
    # 본다 — 실제로 파괴된 카드의 좌표로 두 번 드래그했다(ARTEL-663).
    gone_at: dict[str, int] = Field(default_factory=dict)
    # `deactive` 로 온 객체와 그것이 꺼진 `reading`. `gone` 과 같은 이유로 남긴다 — `render` 가
    # 꺼진 것을 안 그리므로(누를 수 없어 조준 후보가 아니다) 꺼짐도 침묵으로만 표현됐고, 침묵은
    # 소식 없음과 구분이 안 된다. 조합 zone 이 닫힌 줄 모르고 없는 칸에 카드를 끌었다(ARTEL-665).
    off_at: dict[str, int] = Field(default_factory=dict)
    # 마지막으로 그린 판독 번호. 이 장부를 여기 두는 이유는 "무엇까지 보여줬나" 가 이 메모리
    # 자신의 사정이기 때문이다 — 부르는 쪽마다 따로 세면 둘이 어긋날 자리가 생긴다.
    drawn: int = 0
    # 전량 판독이 도착했고 아직 그것을 전량으로 그리지 않았다.
    #
    # 씬이 바뀌면 SDK 가 `whole` 을 보낸다. 그때 한 번은 전량으로 그려야 독자가 새 화면을
    # 한 덩어리로 받는다 — 델타만 이어 붙이면 씬이 바뀐 자리를 지나 거슬러 읽어야 한다.
    # 판독이 이미 그 경계를 말해 주므로 여기서 새 규칙을 만들지 않는다.
    page_due: bool = False

    @property
    def seen(self) -> bool:
        return self.readings > 0

    def clock(self) -> int:
        """`window` 를 재는 자. **`reading` 번호**다.

        둘을 섞고 있었다. 값에 찍는 것은 `self.readings`(적용 횟수, 한 자리~수백)였는데
        `window` 의 시작점을 주는 `after_frame` 은 **`reading` 번호**(1,000·4,283 …)를
        돌려준다. 그래서 `at > since` 가 `3 > 1010` 이 되어 늘 거짓이었다 — 행위 직후
        `render` 에서 값이 한 줄도 안 나오고, `gone` 된 객체도 조용히 없어졌다(ARTEL-665).

        `reading` 번호를 쓴다. `ReadingLog.reading` 과 `after_frame` 이 이미 그것을 쓰고,
        그 셋이 같은 자를 써야 `window` 가 뜻을 갖는다.

        번호를 안 주는 옛 SDK 에서는 적용 횟수로 떨어진다. 그때는 `after_frame` 도 `None` 을
        돌려주므로 `window` 가 `drawn` 으로 가고, 그 안에서 다시 자가 하나로 맞는다.
        """
        return self.reading if self.reading is not None else self.readings

    def after_frame(self, frame: int | None) -> int | None:
        """그 프레임보다 뒤에 잡힌 판독만 남기는 창의 시작점. 모르면 `None`.

        판독과 액션 결과가 같은 시계(`Time.frameCount`)를 쓰므로, 액션이 끝난 프레임보다
        뒤인 판독이 곧 "내 행위 이후"다. 시간으로 어림잡던 것을 이것이 대신한다 — 읽기와
        전달이 두 속도라, 액션 직후 도착하는 배치가 액션 전에 잡힌 것일 수 있었다.

        `None` 을 돌려주는 경우가 둘이다. 프레임을 모르는 옛 SDK, 그리고 그 프레임이 남은
        로그보다 오래된 경우. **둘 다 거짓말보다 낫다** — 부르는 쪽이 종전의 창으로
        돌아가면 될 뿐이고, 없는 경계를 지어내면 판독을 통째로 잃는다.
        """
        if frame is None or not self.log:
            return None

        # 로그는 오래된 것이 앞이다. 그 프레임 이하인 마지막 판독이 창의 시작점 —
        # 그것까지는 액션 이전이고, 그 뒤가 결과다.
        cutoff = None
        for entry in self.log:
            if entry.frame is None or entry.reading is None:
                continue
            if entry.frame <= frame:
                cutoff = entry.reading
            else:
                break

        if cutoff is not None:
            return cutoff

        # 남은 로그가 전부 그 프레임보다 뒤다. 액션이 로그 창 밖으로 밀려났다는 뜻이라
        # 어디까지가 결과인지 말할 수 없다.
        return None

    def apply(self, reading: PulseReading) -> None:
        self.readings += 1

        # A whole reading replaces rather than adds. Keeping the old rows would
        # leave behind objects the game has since destroyed, and the reading
        # says outright that this is everything it can see.
        if reading.whole:
            self.wholes += 1
            self.held = {}
            self.statics = {}
            # 전량 판독은 쥔 것을 통째로 갈아치운다. 그 앞에서 무엇이 사라졌다는 말은 새
            # 화면에 대고 할 말이 아니다.
            self.gone_at = {}
            self.off_at = {}
            self.page_due = True

        if reading.scene is not None:
            self.scene = reading.scene
        for field in ("reading", "frame", "watching", "unresolved", "unwatchable"):
            value = getattr(reading, field)
            if value is not None:
                setattr(self, field, value)

        # 이 `reading` 의 번호. 값에 찍는 것과 `window` 를 재는 것이 같은 자를 쓰게 한다.
        now = self.clock()

        for key in reading.changed:
            self.moves[key] = self.moves.get(key, 0) + 1
        if len(self.moves) > MAX_TRACKED_KEYS:
            # Oldest-first is not available here, so drop the quietest: a key
            # that moved once is the least informative thing to keep.
            for key in sorted(self.moves, key=self.moves.get)[: len(self.moves) - MAX_TRACKED_KEYS]:
                del self.moves[key]

        self.log.append(
            ReadingLog(
                reading=reading.reading,
                frame=reading.frame,
                whole=reading.whole,
                changed=list(reading.changed[:MAX_CHANGED_NAMED]),
                moved=len(reading.changed),
            )
        )
        if len(self.log) > MAX_READING_LOG:
            # 앞에서 버린다. 최신이 가장 쓸모 있고, 오래된 판독은 이미 접혀 상태에 들어갔다.
            del self.log[: len(self.log) - MAX_READING_LOG]
            self.trimmed = True

        for entry in reading.statics:
            self.statics[entry.key] = entry
            self.static_at[entry.key] = now

        # Which list an object arrives in is what says whether it is on. It is
        # not a field on the object, so it cannot disagree with itself.
        for live, arrived in ((True, reading.active), (False, reading.deactive)):
            for folded in arrived:
                obj = folded.flatten(reading.scene)
                was = self.held.get(obj.key)
                held = _HeldObject(
                    scene=obj.scene or (was.scene if was else None),
                    # 0 이 유효한 id 는 아니지만 None 과 가리려면 is not None 이어야 한다.
                    id=obj.id if obj.id is not None else (was.id if was else None),
                    path=obj.path or (was.path if was else None),
                    selector=obj.selector or (was.selector if was else None),
                    world=obj.world or (was.world if was else None),
                    rect=obj.rect or (was.rect if was else None),
                    tag=obj.tag or (was.tag if was else None),
                    text=obj.text if obj.text is not None else (was.text if was else None),
                    offers=obj.offers or (was.offers if was else None),
                    live=live,
                    members=dict(was.members) if was else {},
                )
                held.at = dict(was.at) if was else {}
                # 실려 왔다는 것이 곧 바뀌었다는 뜻이다 — SDK 가 장부에 얹어 보내므로
                # 안 바뀐 글자는 델타에 없다. 안 실려 왔으면 옛 시각을 그대로 이어받는다.
                held.text_at = now if obj.text is not None else (was.text_at if was else 0)
                # 말한 기록도 이어받는다. 여기서 잃으면 이미 말한 값을 매 턴 다시 말한다.
                held.shown = dict(was.shown) if was else {}
                for member in obj.members:
                    held.members[member.key] = member
                    held.at[member.key] = now
                self.held[obj.key] = held

                # 켜져 있던 것이 꺼진 **순간**만 적는다. 처음 보는데 이미 꺼져 있는 것은
                # 소식이 아니라 사정이고, 그것은 `whole` 페이지가 한 번에 센다.
                if not live and was is not None and was.live:
                    self.off_at[obj.key] = now
                elif live:
                    self.off_at.pop(obj.key, None)

        # 사라졌다고 한 것을 놓는다. 꺼진 것과 달리 **지운다** — 꺼진 것은 다시 켜지면 판독이
        # `active` 통에 넣어 보내지만, 파괴된 것은 영영 안 온다.
        #
        # 판독이 말한 것만 지운다. 통에 안 왔다는 이유로 지우면 판독이 잘린 창에서 살아 있는
        # 객체를 잃는다 — 안 걸은 것과 없는 것은 걷는 쪽만 가릴 수 있고, 그래서 이 목록이
        # 생겼다(ARTEL-651).
        for key in reading.gone:
            self.held.pop(key, None)
            # 꺼진 채로 파괴된 것이다. 두 번 말하지 않는다 — `gone` 이 더 센 말이다.
            self.off_at.pop(key, None)
            # 지운 것으로 끝내지 않는다. 렌더가 이것을 말한다 — 지우기만 하면 읽는 쪽은
            # 대화에 남은 옛 화면에서 그 객체를 계속 본다.
            self.gone_at[key] = now
            # 이동 횟수도 함께 놓는다. selector 는 자리 번호라 다음 카드가 같은 이름으로
            # 태어날 수 있고, 그러면 새 객체가 죽은 객체의 "일곱 판독에서 움직였다"를 물려받는다.
            for tracked in [name for name in self.moves if name.startswith(f"{key}|")]:
                del self.moves[tracked]

        # 오래된 것부터 버린다. 이미 말한 것은 대화에 남아 있다.
        for ledger in (self.gone_at, self.off_at):
            if len(ledger) <= MAX_TRACKED_KEYS:
                continue
            for key in sorted(ledger, key=ledger.get)[: len(ledger) - MAX_TRACKED_KEYS]:
                del ledger[key]

    def render(
        self,
        since: int | None = None,
        advance: bool = True,
        news_since: int | None = None,
    ) -> str | None:
        """The pulse view, or None when no reading has arrived.

        `since` 는 마지막으로 그린 판독 번호다. 그 뒤에 도착한 값만 그린다 — 판독은 이미
        델타라 도착했다는 것 자체가 움직였다는 뜻이고, 도착하지 않은 값은 독자가 지난번에
        이미 읽었다.

        **조작할 수 있는 것은 창과 무관하게 매번 그린다.** 그것이 없으면 에이전트가 무엇을
        누를 수 있는지 물어봐야 하고, 그 왕복이 이 채널이 없애려던 것이다. 대신 값은 싣지
        않는다 — 안 움직인 값은 이미 알고 있다.

        `since=0` 은 종전과 같은 전량이다. 처음 보는 독자에게는 전부가 새것이다.

        None is what keeps an SDK that sends no pulse reading exactly as it was:
        nothing is appended, so every prompt is byte-identical to before.
        """
        if not self.seen:
            return None

        # 부르는 쪽이 창을 정하지 않으면 지난번에 그린 자리부터다. 그리고 나면 그 자리를
        # 옮긴다 — 같은 값을 두 번 그리지 않기 위해서다.
        #
        # **창을 옮기는 것은 이 경로뿐이다.** 라이브 뷰는 [render_now] 로 가고 그쪽은 옮기지
        # 않는다. 둘 다 여기로 오던 때, 한 턴에 도구 결과가 먼저 그려 창을 먹고 나면 그 뒤에
        # 붙는 라이브 뷰가 비어서 올라갔다(ARTEL-579).
        if since is None:
            since = self.drawn
        # 무엇을 그릴지(`since`)와 무엇이 소식인지(`news_since`)는 다른 질문이다. 창 뷰에서는
        # 둘이 같아서 그릴 것이 곧 소식이지만, 전량 뷰에서는 전부 그리면서 소식만 표시해야
        # 한다 — `since=0` 을 소식 기준으로도 쓰면 모든 줄에 표가 붙어 표가 뜻을 잃는다.
        marking = news_since is not None and news_since != since
        if news_since is None:
            news_since = since
        if advance:
            self.drawn = self.clock()

        lines = [PULSE_VIEW_START]
        head = f"reading {self.reading}"
        if self.frame is not None:
            head += f" · frame {self.frame}"
        if self.scene:
            head += f" · scene {self.scene}"
        lines.append(head)
        if self.unwatchable:
            # Said out loud so "nothing moved" is not read as "nothing is there".
            lines.append(f"could not read: {self.unwatchable}")

        # 로그도 창을 탄다. 지난번에 그린 판독을 다시 적으면 그만큼이 매 턴 반복된다.
        #
        # 그리고 **한 종류만 계속 움직이는 판독은 한 줄로 접는다.** 실측에서 전투 중 변화의
        # 98%가 `SlimeAnimator::spriteRenderer` 하나였다 — 애니메이션이 스프라이트를 갈아
        # 끼우는 것이지 QA 가 판정할 값이 아닌데, 그것이 로그 열 줄을 독차지했다.
        window = [e for e in self.log if (e.reading or 0) > since] or self.log[-1:]
        if window:
            lines.append("")
            head = f"readings since you last looked (last {MAX_READING_LOG} kept)"
            if self.trimmed:
                # 열 개가 전부라고 읽으면 그 앞을 없었던 일로 세게 된다.
                head += " — earlier readings dropped"
            lines.append(head + ":")
            # 객체가 아니라 **멤버**로 접는다. 같은 애니메이션이 적 다섯에서 돌면 판독마다
            # 대상이 달라 객체로는 안 접히는데, 말하는 내용은 하나다.
            members = {k.rsplit("|", 1)[-1] for e in window for k in e.changed}
            if len(window) > 2 and 0 < len(members) <= 2:
                said = ", ".join(sorted(members))
                where = {k.rsplit("|", 1)[0].rsplit("/", 1)[-1] for e in window for k in e.changed}
                lines.append(
                    f"  {len(window)} readings, only {said} moved"
                    + (f" (on {len(where)} objects)" if len(where) > 1 else "")
                )
            else:
                for entry in window:
                    lines.append(f"  {entry.reading} ({self._log_line(entry)})")
            lines.append("")

        # 사라진 것을 말한다. 창을 타므로 한 번 말하고 만다.
        #
        # 자리는 판독 로그 다음, 화면 앞이다 — 무엇이 없어졌는지가 지금 화면을 읽는 전제다.
        # 문구는 GAME_STATE 갈래(`scene.py` 의 `missing`)와 같은 것을 쓴다. 두 채널이 같은
        # 것을 다르게 말하면 읽는 쪽이 둘을 다른 사실로 읽는다.
        gone = [key for key, at in self.gone_at.items() if at > since]
        if gone:
            lines.append(f"gone from the scene: {self._roll(gone)}")

        # `deactive` 도 같은 이유로 말한다. `render` 가 꺼진 객체를 안 그리므로(누를 수 없어
        # 조준 후보가 아니다) 꺼짐 역시 침묵으로만 표현됐고, 침묵은 소식 없음과 구분이 안 된다.
        # 조합 zone 이 닫힌 줄 모르고 없는 칸에 카드를 끌었다.
        #
        # `gone` 과 다른 말이다. 꺼진 것은 다시 켜지면 `pulse` 가 `active` 통에 넣어 보내므로
        # 저절로 돌아온다 — 그래서 "없어졌다" 가 아니라 "지금은 꺼져 있다" 다.
        off = [key for key, at in self.off_at.items() if at > since]
        if off:
            lines.append(f"switched off: {self._roll(off)}")

        # `whole` 페이지는 **지금 꺼져 있는 것 전부**를 한 번 센다. 페이지는 켜진 것만 그리므로,
        # scene 에 막 들어온 독자는 무엇이 있는데 꺼져 있는지를 알 길이 없다. 이름만이고 값은
        # 없다 — 꺼진 동안의 값은 `pulse` 도 안 보낸다.
        dark = [k for k, held in self.held.items() if not held.live] if since == 0 else []
        if dark:
            lines.append(f"here but switched off: {self._roll(dark)}")

        if gone or off or dark:
            lines.append("")

        # 화면의 글자는 **매번 전부** 그린다. 아래 `statics` 와 같은 이유다(ARTEL-690).
        #
        # 종전에는 객체 블록 안에서 `says '...'` 한 줄로, 창(window)을 타고 나갔다. 그것이
        # 틀렸다. 글자는 바뀔 때만 델타에 실리므로 창으로 가르면 한 번 그리고 끝인데, `pulse`
        # block 은 scene view marker 안에 들어가고(`app/qa/scene.py`) `DEFAULT_KEEP_SCENES = 1`
        # (`app/agents/qa/context.py`)이라 다음 도구 결과가 오는 순간 접힌다. 그래서 화면의
        # 글자는 모델 호출 딱 한 번 동안만 문맥에 있었고, 떠 있는 내내 안 변하는 튜토리얼
        # 안내문은 그 뒤로 영영 안 보였다 — 에이전트가 화면이 시키는 것을 안 따른 원인이다.
        #
        # `statics` 와 성질이 같다: 안 변하는 동안에도 그것이 지금 무엇을 해야 하는지를
        # 결정한다. 그래서 같은 거래를 한다 — 창을 안 타는 대신 상한(`MAX_SCREEN_TEXT`)을 둔다.
        #
        # 글자가 앞, 주소가 뒤다. 읽을 것은 글자이고 주소는 그것을 `inspect_object` 로 펴거나
        # 겨누기 위한 것이다.
        readable = [
            (key, held)
            for key, held in self.held.items()
            # 꺼진 객체의 글자는 화면에 없다. SDK 는 꺼진 객체에도 글자를 싣는다 —
            # `silent` 이 멤버 값에만 걸리기 때문이라, 거르는 것은 이쪽 몫이다.
            if held.live and (held.text or "").strip()
        ]
        if readable:
            readable.sort(key=self._reading_order)
            shown = readable
            hidden = 0
            if len(readable) > MAX_SCREEN_TEXT:
                # 넘치면 바뀐 줄을 먼저 살린다. `_roll` 이 꺼진 객체에 하는 거래와 같다 —
                # 그때 읽는 쪽이 찾는 것은 전체 명단이 아니라 방금 무엇이 달라졌나다.
                changed_indexes = [
                    index
                    for index, (_, held) in enumerate(readable)
                    if held.text_at > news_since
                ][:MAX_SCREEN_TEXT]
                quiet_indexes = [
                    index
                    for index, (_, held) in enumerate(readable)
                    if held.text_at <= news_since
                ]
                kept_indexes = changed_indexes + quiet_indexes[
                    : MAX_SCREEN_TEXT - len(changed_indexes)
                ]
                # 고르는 것과 적는 것을 가른다. 살아남은 줄은 다시 읽는 순서로 적는다.
                shown = [readable[index] for index in sorted(kept_indexes)]
                hidden = len(readable) - len(shown)
            lines.append("the screen reads:")
            for key, held in shown:
                where = held.selector or held.path or key
                # 멤버 줄과 달리 `marking` 을 안 건다. 이 절은 창과 무관하게 매번 전부
                # 그리므로, 표가 없으면 창 뷰에서도 어느 줄이 이번에 바뀐 것인지 알 수 없다.
                # 그리고 그 표가 "Space 를 눌렀는데 대사가 넘어갔는가" 를 가리는 값이다.
                news = "  (changed)" if held.text_at > news_since else ""
                lines.append(f"  {held.text!r}   {where}{self._aim(held)}{news}")
            if hidden:
                # 잘랐다는 것을 말한다. 조용히 자르면 독자가 이것을 화면의 전부로 읽는다.
                lines.append(f"  (+{hidden} more labels on screen; inspect_object shows what one says)")
            lines.append("")

        # statics 는 **매번 전부** 그린다. 이 절과 위의 화면의 글자 절만 창(window)을 안 따른다.
        #
        # 소유자가 없기 때문이다. 객체 멤버는 그 객체가 아래에 그려지면서 함께 보이지만,
        # static 은 어느 객체 아래에도 안 실린다 — 델타에서 빠지는 순간 화면 어디에도 없다.
        #
        # 그리고 static 으로 뽑히는 값이 대체로 **래칭**이다. 한 번 켜지면 꺼질 때까지 유지되고,
        # 그 사이 내내 무엇을 할 수 있는지를 결정한다. 샘플 게임의 `InteractionLock.IsLocked` 가
        # 그렇다: 대화창이 떠 있는 동안 카드 드래그가 통째로 취소되는데, 그 사실이 잠기는 순간
        # 한 번만 보이고 사라졌다. 다음 턴의 에이전트는 잠긴 줄 모르고 끌었고, 카드가 튕기는
        # 것을 좌표가 틀린 것으로 읽었다(ARTEL-573).
        #
        # 값이 열한 개다(실측). 창이 아끼는 것과 견줄 크기가 아니다.
        if self.statics:
            lines.append("statics:")
            for key in sorted(self.statics):
                entry = self.statics[key]
                name = f"{(entry.declaring or '').split('.')[-1]}.{entry.member}"
                # 마지막으로 본 뒤 바뀐 것만 표시한다. 전부 그리면서 표시까지 없으면 읽는 쪽이
                # 무엇이 소식인지 스스로 찾아야 하고, 그것이 창이 하라고 있는 일이다.
                news = "  (changed)" if self.static_at.get(key, 0) > news_since else ""
                lines.append(f"  {name} = {entry.value!r}{news}{self._moved(key)}")

        objects = sorted(self.held.items())
        for key, obj in objects:
            # 꺼진 것은 그리지 않는다. 화면에 없고 누를 수도 없어 조준 후보가 아니고,
            # GAME_STATE 도 활성 GameObject 만 보냈다 — 여기서 그리면 그것보다 못해진다.
            #
            # 들고 있기는 한다. 판독이 "그건 꺼졌다" 고 말한 것 자체가 사실이고, 다시 켜지면
            # 판독이 그 객체를 active 통에 넣어 보내므로 저절로 돌아온다. held 에서 지우면
            # 그 사이 값을 잃고 다시 켜질 때 전량 판독을 기다리게 된다.
            #
            # 풀에서 꺼내 쓰는 게임이 이것으로 산다: 카드 스무 장짜리 풀에서 손에 든 셋만
            # 활성이면, 프롬프트에 드는 것도 셋이다.
            if not obj.live:
                continue
            # 멤버가 없어도 쓴다. 판독이 유일한 출처일 때, 조준값과 무엇을 할 수 있는지만
            # 들고 오는 객체가 대다수다 — 그것을 버리면 누를 것이 화면에서 사라진다.
            #
            # 글자만 든 라벨은 여기서 걸러도 된다. 위의 화면의 글자 절이 그 글자와 주소를
            # 이미 싣고, 여기서 블록을 하나 더 만들면 같은 주소가 두 번 나온다(ARTEL-690).
            if not obj.members and not obj.offers and obj.id is None:
                continue

            fresh = [k for k in sorted(obj.members) if obj.at.get(k, 0) > since]
            # 창 밖에서 움직였는데 아직 한 번도 안 말한 값. 창의 경계는 마지막 **행위**이고
            # 이것은 **내가 무엇을 말했나** 라, 둘은 다른 질문이다(ARTEL-662).
            owed = [
                k
                for k in sorted(obj.members)
                if k not in fresh and obj.at.get(k, 0) > obj.shown.get(k, 0)
            ]
            # 조작할 수 있다는 것은 **무엇을 할지 아는 것**이다. id 는 거의 모든 객체에
            # 실리므로 그것으로 가르면 아무것도 안 걸러진다 — offers 가 그 선이다.
            actionable = bool(obj.offers)

            # 이번 창에 아무 말도 없고, 아직 안 말한 값도 없고, 누를 수도 없는 객체는
            # 건너뛴다. 독자가 이미 아는 것을 다시 적는 자리다.
            #
            # `owed` 가 여기 있어야 창 밖에서 값이 변한 객체가 보인다. 그것이 없던 때
            # 159턴 런에서 판독이 이름 댄 멤버 53종 중 23종이 한 번도 값을 못 냈다(ARTEL-662).
            #
            # 글자는 이 판단에 안 든다. 화면의 글자 절이 창과 무관하게 전부 그리므로,
            # 글자만 든 객체는 여기서 블록을 안 만드는 것이 맞다(ARTEL-690).
            if not fresh and not owed and not actionable:
                continue

            where = obj.selector or obj.path or key
            lines.append(f"{where}{self._aim(obj)}:")
            offered = self._offered(obj)
            if offered:
                lines.append(f"  {offered}")
            for member_key in fresh + owed:
                member = obj.members[member_key]
                name = f"{(member.on or '').split('.')[-1]}.{member.member}"
                asked = "" if member.asked is not False else " (unasked)"
                moved = self._moved(f"{key}|{member_key}", f"{where}|{member_key}")
                # 창 뷰에서는 그려진 것이 곧 소식이라 표가 군더더기다. 전량 뷰에서만 붙인다.
                news = (
                    "  (changed)"
                    if marking and obj.at.get(member_key, 0) > news_since
                    else ""
                )
                # 창 밖에서 변한 값은 표를 단다. 창의 뜻이 "이 행위가 만든 것" 이므로, 옛
                # 값을 표 없이 섞으면 읽는 쪽이 그것을 방금 일어난 일로 읽는다.
                earlier = "  (changed earlier)" if member_key in owed else ""
                lines.append(f"  {name} = {member.value!r}{asked}{news}{earlier}{moved}")
                if advance:
                    obj.shown[member_key] = self.clock()

        lines.append(PULSE_VIEW_END)
        return "\n".join(lines)

    def inspect(self, selector: str) -> str:
        """한 객체가 쥔 값 전부. 창과 무관하게 지금 아는 것을 다 적는다.

        상시 블록이 변화와 조작 가능한 것만 싣게 되면서 생긴 짝이다. 안 움직인 값은 매 턴
        적지 않되, 물으면 답할 수 있어야 한다 — 그러지 않으면 줄인 것이 아니라 잃은 것이다.

        부분 일치를 받는 이유: 블록에 적히는 주소는 `RangedCat(Clone)[17]` 처럼 길고, 대괄호
        안 숫자는 씬을 다시 걸을 때마다 달라질 수 있다. 정확히 옮겨 적기를 요구하면 그 자체가
        왕복을 만든다.
        """
        needle = (selector or "").strip().lower()
        if not needle:
            return "Name the object you want to inspect."

        hits = [
            (key, obj)
            for key, obj in sorted(self.held.items())
            if needle in (obj.selector or "").lower() or needle in (obj.path or "").lower()
        ]
        if not hits:
            return (
                f"No object matching {selector!r}. The scene block lists what is there; "
                "the address printed beside each one is what this takes."
            )

        lines = []
        for key, obj in hits[:MAX_INSPECTED]:
            where = obj.selector or obj.path or key
            state = "" if obj.live else "  (switched off)"
            lines.append(f"{where}{self._aim(obj)}{state}:")
            # 물으면 답한다 — 창과 무관하게 지금 아는 것을 다 적는 자리라, 이미 말한
            # 글자라도 여기서는 다시 낸다(ARTEL-690).
            if obj.text is not None:
                lines.append(f"  says {obj.text!r}")
            offered = self._offered(obj)
            if offered:
                lines.append(f"  {offered}")
            if not obj.members:
                lines.append("  (no values are being read on this object)")
            for member_key in sorted(obj.members):
                member = obj.members[member_key]
                name = f"{(member.on or '').split('.')[-1]}.{member.member}"
                asked = "" if member.asked is not False else " (unasked)"
                lines.append(f"  {name} = {member.value!r}{asked}")
        if len(hits) > MAX_INSPECTED:
            # 잘랐다는 것을 말한다. 조용히 자르면 독자가 이것을 전부로 읽는다.
            lines.append(f"({len(hits) - MAX_INSPECTED} more objects match; name one more exactly)")
        return "\n".join(lines)

    def current_scene(self) -> str | None:
        """지금 쥔 것 전부. 청했을 때만 그린다.

        `since_action` 이 주는 것은 **마지막 행위 이후 달라진 것**이라, 한 번 말하고 가만히
        있는 값은 안 나온다. 그래서 부르는 쪽이 값을 다시 보려면 `inspect_object` 로 물어야
        하는데 그것은 `selector` 를 받는다 — **그려지지 않은 객체는 주소를 알 길이 없다.**
        실제 런에서 `Card(Clone)` 을 찍어서 묻고 없다는 답을 받았다(ARTEL-673).

        `since=0` 이라 모든 값이 그려지고, `news_since` 가 그중 새 것에만 표를 단다. 전부
        그리면서 표까지 없으면 읽는 쪽이 무엇이 소식인지 스스로 찾아야 한다.

        scene 전환이 예약한 페이지(`page_due`)는 **소진하지 않는다.** 그것은 새 화면을 한
        덩어리로 주기로 한 약속이고, 여기서 먹으면 그 약속이 조용히 사라진다.

        게임에 아무것도 안 묻는다. SDK 의 `whole` 은 "게임이 전부 보냈다" 이고 이것은 "내가
        아는 전부를 그린다" 라, 같은 말을 쓰면 새로 청하는 것으로 읽힌다.
        """
        return self.render(since=0, news_since=self.drawn)

    def since_action(self, frame: int | None) -> str | None:
        """행위 하나가 무엇을 남겼나. 씬이 바뀌었으면 전량 한 페이지로.

        도구 결과에 실리는 것이 이것이다. 도구 결과는 대화에 남으므로, 여기 그린 것은
        지워지지 않는다 — 한 번 말하면 그 자리에 있다.

        **매 턴 교체되는 꼬리를 대신한다.** 종전에는 `render_now()` 를 모델 호출 맨 뒤에
        붙였는데, 그것이 프롬프트 접두를 매 턴 깨뜨려 캐시가 시스템 프롬프트에서 멈췄다.
        크기나 내용의 문제가 아니었다 — 10 토큰짜리 고정 꼬리도 똑같이 깼다(ARTEL-621).

        씬이 바뀐 뒤 첫 호출은 **전량**이다. 새 화면을 한 덩어리로 주지 않으면 독자가 씬
        경계를 지나 거슬러 읽어야 한다. 그 다음부터는 델타이고, 앞의 페이지가 대화에 남아
        있으므로 잃는 것이 없다.

        `frame` 은 이 행위가 끝난 Unity 프레임이다. 그보다 뒤에 잡힌 판독만이 이 행위의
        결과다 — 0.1초 떴다 사라진 것도 거기 남는다. 모르면(옛 SDK, 또는 그 프레임이 로그
        창 밖) 종전처럼 마지막으로 그린 자리부터 그린다.
        """
        if self.page_due:
            self.page_due = False
            return self.render(since=0, news_since=self.drawn)

        return self.render(since=self.after_frame(frame))

    @staticmethod
    def _aim(obj: "_HeldObject") -> str:
        """조준에 필요한 것: 무엇으로 지목하고 화면 어디인가.

        selector 는 사람이 읽는 주소이고 액션이 받는 것은 아직 id 다. 둘을 함께 쓰는 것이
        ARTEL-480 이 끝나기 전까지의 정직한 모양이다 — 하나만 주면 독자가 나머지를 물어야 한다.
        """
        bits = []
        if obj.id is not None:
            bits.append(f"id={obj.id}")

        # 게임이 이 객체를 무엇으로 분류해 두었는가(ARTEL-631). 조준값 옆이 그 자리다 —
        # 무엇을 겨눌지 고를 때 필요한 것이고, 실제로 어느 카드가 어느 조합 칸에 들어가는지가
        # 이것으로 갈린다. 그것을 몰라 에이전트가 카드를 반대로 넣었다.
        if obj.tag:
            bits.append(f"tag={obj.tag}")

        # 모서리가 아니라 **중심**을 낸다. 판독이 싣는 `rect.x/y` 는 요소의 좌상단이고,
        # 포인터 도구가 받는 것은 겨눌 점이다. 그대로 내면 276px 짜리에서 138px 씩
        # 어긋나고, 읽는 쪽은 그것이 모서리인 줄 모르므로 어긋난 자리에서 드래그를
        # 시작한다(ARTEL-569).
        #
        # 표기도 `@` 로 맞춘다. `app/qa/scene.py` 의 `_where` 가 같은 뜻을 같은 모양으로
        # 내고 있었고, 여기만 `at` 이었다. 두 줄이 같은 것을 뜻하는 척하면서 다른 것을
        # 뜻하는 상태였다.
        rect = obj.rect or {}
        corner = [rect.get(k) for k in ("x", "y", "w", "h")]
        if all(isinstance(value, (int, float)) for value in corner):
            x, y = center_of(*corner)
            bits.append(f"@ {x},{y} {int(corner[2])}x{int(corner[3])}")
        return f"  [{' · '.join(bits)}]" if bits else ""

    @staticmethod
    def _reading_order(item: tuple[str, "_HeldObject"]) -> tuple[int, float, float, str]:
        """사람이 화면을 읽는 순서. 위에서 아래로, 같은 높이면 왼쪽부터.

        화면 좌표의 원점이 좌상단이라(`app/qa/envelope.py` 의 `Rect`) y 가 작은 것이 화면
        위쪽이다. 판독이 싣는 `rect.x/y` 는 요소의 좌상단이고, 여기서는 중심으로 고칠 이유가
        없다 — 겨누는 것이 아니라 줄을 세우는 것이라 두 좌표계가 같은 순서를 낸다.

        `rect` 가 없거나 x/y 가 수가 아닌 객체는 어디 있는지 모르는 것이라 맨 뒤에 두고, 그
        안에서는 주소 순으로 세운다. 순서가 판독마다 흔들리면 같은 화면이 매번 다르게 읽힌다.
        """
        key, obj = item
        where = obj.selector or obj.path or key
        rect = obj.rect or {}
        x, y = rect.get("x"), rect.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return 1, 0.0, 0.0, where
        return 0, float(y), float(x), where

    @staticmethod
    def _offered(obj: "_HeldObject") -> str:
        """이 객체에 무엇을 할 수 있나.

        스캔이 볼 수 없는 둘이 여기 있다(`WatchList.Offer`) — 어떤 키가 뜻을 가지는가, 그리고
        어떤 객체가 포인터에 답하는가. 배선된 메서드 이름까지 함께 쓴다: 누른 뒤 아무것도
        움직이지 않았을 때, 무엇이 불렸어야 하는지를 아는 독자만 그것을 결함으로 부를 수 있다.
        """
        offers = obj.offers or {}
        parts = []
        clicks = offers.get("clicks") or []
        for call in clicks:
            on = call.get("on") or "?"
            method = call.get("method") or "?"
            parts.append(f"click → {on}.{method}")
        keys = offers.get("keys") or []
        if keys:
            parts.append("keys: " + ", ".join(_key_line(k) for k in keys))
        pointers = offers.get("pointers") or []
        if pointers:
            parts.append("pointer: " + ", ".join(str(p) for p in pointers))
        return "can do — " + " · ".join(parts) if parts else ""

    @staticmethod
    def _log_line(entry: ReadingLog) -> str:
        """로그 한 줄의 본문.

        전량 판독은 키를 이름 대지 않는다. 직전이 없어 **감시 중인 전부**가 움직인 것으로
        들어오므로, 그 목록은 "무엇이 움직였나"에 답하지 않고 "무엇을 보고 있나"에 답한다 —
        그것은 이 줄이 묻는 것이 아니다.
        """
        if entry.whole:
            return f"whole — {entry.moved} values reported"
        if not entry.moved:
            return "delta — nothing moved"
        named = ", ".join(entry.changed)
        rest = entry.moved - len(entry.changed)
        return f"delta — {named}" + (f", +{rest} more" if rest > 0 else "")

    def _roll(self, keys: list[str]) -> str:
        """이름 몇 개, 그리고 몇 개가 더 있는지. 명단이 아니라 사실을 준다.

        scene 이 헐리는 순간에는 수십이 한꺼번에 꺼진다. 그것을 다 적으면 그 한 줄이 `render`
        전체보다 길어지는데, 읽는 쪽에 필요한 것은 "무엇이 꺼졌나" 몇 개와 "많이 꺼졌다" 는
        사실이다.
        """
        named = sorted(self._named(k) for k in keys)
        if len(named) <= MAX_NAMED_OFF:
            return ", ".join(named)
        return ", ".join(named[:MAX_NAMED_OFF]) + f", +{len(named) - MAX_NAMED_OFF} more"

    def _named(self, key: str) -> str:
        """객체를 부르는 이름. 화면의 머리줄과 같은 모양으로.

        `held` 의 키는 `씬/selector` 인데 객체는 `selector` 로 그려진다. 사라진 것만 다른
        모양으로 부르면 읽는 쪽이 그것을 같은 객체로 못 알아본다. 다른 씬의 것은 씬을 달고
        있어야 하므로 지금 씬일 때만 뗀다.
        """
        prefix = f"{self.scene}/" if self.scene else None
        if prefix and key.startswith(prefix):
            return key[len(prefix) :]
        return key

    def _moved(self, *keys: str) -> str:
        """How many readings this key moved in, when that is more than one.

        The reading names a moved key with its own spelling, which this cannot
        reconstruct exactly — so every candidate is tried and the first hit
        wins. A miss costs the annotation, not the value.
        """
        for key in keys:
            count = self.moves.get(key)
            if count and count > 1:
                return f"  [moved in {count} readings]"
        return ""
