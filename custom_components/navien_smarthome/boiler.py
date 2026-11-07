"""보일러(스마트톡) 기기 모델 — **관찰(수신) 전용.**

매트(AWS shadow)·에어원(cmd/rc)과 또 다른 세 번째 체계다. 기기는 요청-응답으로만
동작하며(30분 방치 실측: 자발 보고 0건), 상태는 `{homeSeq}/smarttok/{mac+부가값}`
토픽에 아래 봉투로 온다 (NR-65D 실측, 2026-08-03).

    {"topic": "cmd/20/.../res",
     "payload": {"protocolVersion": 1, "clientID": "roomcon-...",
                 "requestTopic": "cmd/20/roomcon-.../control",
                 "response": {"deviceType": 20, "macAddress": "...",
                              "additionalValue": "...",
                              "status": { 평면 키-값 }}}}

**이 모듈은 제어하지 않는다.** 상태 요청도 보내지 않는다 — 다른 클라이언트(앱·
SmartThings)가 조회할 때 오는 응답을 받아 보여줄 뿐이다. 제어와 능동 폴링은
실기기 드라이런 검증 뒤에 연다. 가스 기기다 — 추측으로 보내지 않는다.

온도 스케일은 실측 교차검증으로 확정했다:

- 설정·측정 온도 대부분: **0.5°C 단위 정수** (insideTemperature=53 → 26.5°C)
- `actualInsideTemperature`: **0.1°C 단위** (267 → 26.7°C — 위와 26.5≈26.7 로 교차확인)
- `outsideTemperature`: 0.1°C 로 **추정** (280 → 28.0°C, 8월 외기와 부합. 미확정)

각방(멀티존) 비트마스크는 다루지 않는다 — 단일 존 실측만 있다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)

# 진단에 남길 상태 기록 개수. 순서를 보는 것이 목적이라 길 필요가 없다.
_LOG_KEEP = 8


# `operationMode` — NR-65D 실기기에서 모드를 하나씩 바꿔가며 확인했다 (2026-08-03).
# 각 모드의 설정 범위가 서버 메타데이터와 정확히 일치해 교차검증까지 된 값이다.
#
#   5  실내난방        (실내 설정 10~40°C)
#   6  온돌난방        (난방수 설정 30~65°C)
#   4  온수전용(외출)  (온수 설정 30~60°C) — 룸콘에서 하나의 모드다
#   1  꺼짐
#
# 표에 없는 값은 이름을 지어내지 않는다 — `알 수 없음(N)` 으로 보여 제보를 받는다.
OPERATION_MODE_NAMES: dict[int, str] = {
    1: "꺼짐",
    4: "온수전용(외출)",
    5: "실내난방",
    6: "온돌난방",
}

# **서버가 능력 메타데이터(`Properties.did`)를 주지 않을 때만 쓰는 대체값.**
#
# 2026-10 부터 기기목록 REST 의 `Properties.did` 가 null 로 온다. 그러면
# `feature` 가 비어 난방(climate)·전원(switch) 엔티티가 아예 만들어지지 않았다.
# 값은 NR-65D 서버 선언값 그대로다 (이슈 #17 의 feature, 0.5°C 단위 raw):
#   실내 20~80 (10~40°C) / 온돌 60~130 (30~65°C) / 온수 60~120 (30~60°C)
# `*Use` 플래그는 `did` 가 오던 때 이 기기에서 엔티티가 만들어지던 값(2=지원)이다.
# 모델 이름이 정확히 일치할 때만 쓴다 — 같은 modelCode 20 이라도 NR-60DS 처럼
# 온돌을 지원하지 않는 모델이 있다. 서버 선언이 돌아오면 항상 그쪽이 우선한다.
KNOWN_MODEL_FEATURES: dict[str, dict[str, Any]] = {
    "NR-65D": {
        "powerUse": 2,
        "gooutUse": 2,
        "insideUse": 2,
        "ondolUse": 2,
        "insideTemperatureMin": 20,
        "insideTemperatureMax": 80,
        "ondolTemperatureMin": 60,
        "ondolTemperatureMax": 130,
        "hotWaterTemperatureMin": 60,
        "hotWaterTemperatureMax": 120,
    },
}

# `command` 는 **전체 정수**다. 상위 바이트가 명령의 계열을 가른다.
# 아래 값은 앱의 `BoilerConstants` 에서 그대로 옮긴 것이다 (APK 확인).
COMMAND_SOURCE_REQUEST = 0x01  # 조회 요청 (`*_REQ`)
COMMAND_SOURCE_SETTING = 0x02  # 설정 요청 (`*_SETTING_REQ`) — 응답 에코도 이 계열
COMMAND_SOURCE_DEVICE = 0x04  # 기기 자발 보고 (앱 상수에 없다 — 실측으로만 확인)

# --- 조회 요청 (0x01 계열) — 앱 `BoilerConstants` ---
CMD_START_SERVER = 16777217  # 0x01000001
CMD_CTRL_END = 16777218  # 0x01000002
CMD_STATUS_REQ = 16777219  # 0x01000003  **상태 조회**
CMD_HYO_REQ = 16777220  # 0x01000004
CMD_SAFETY_REQ = 16777222  # 0x01000006
CMD_HOME_OUT_REQ = 16777223  # 0x01000007
CMD_GAS_USABLE_REQ = 16777224  # 0x01000008
CMD_WEEK_RESERVE_REQ = 16777225  # 0x01000009
CMD_FAST_HOTWATER_REQ = 16777226  # 0x0100000A

# --- 설정 요청 (0x02 계열) — 앱 `BoilerConstants` ---
# **우리가 응답에서 관측한 값들과 정확히 일치한다.**
CMD_POWER_OFF = 33554433  # 0x02000001  실측 ✓
CMD_POWER_ON = 33554434  # 0x02000002  실측 ✓
CMD_HOME_OUT_OFF = 33554435  # 0x02000003
CMD_HOME_OUT_ON = 33554436  # 0x02000004  실측 ✓ (외출)
CMD_INNER_HEAT_TEMP = 33554437  # 0x02000005  실측 ✓ (실내난방 온도)
CMD_ONDOL_HEAT_TEMP = 33554438  # 0x02000006  실측 ✓ (온돌 온도)
CMD_REPEAT_RESERVE = 33554439  # 0x02000007
CMD_24H_RESERVE = 33554440  # 0x02000008
CMD_HOT_WATER_ONLY_ON = 33554442  # 0x0200000A
CMD_HOT_WATER_TEMP = 33554443  # 0x0200000B  실측 ✓ (온수 온도)
CMD_FAST_HOT_WATER = 33554444  # 0x0200000C
CMD_HEAT_INTENSITY = 33554445  # 0x0200000D
CMD_WEEK_RESERVE_SETTING = 33554446  # 0x0200000E
CMD_OUTER_TEMP_SETTING = 33554447  # 0x0200000F
CMD_HYO_SETTING = 33554448  # 0x02000010
CMD_SAFETY_SETTING = 33554449  # 0x02000011
CMD_DHW_BOOST_SETTING = 33554460  # 0x0200001C

# `mode` 필드에 들어가는 문자열 — 앱 `ControlRequestMode`.
MODE_POWER_ON = "power-on"
MODE_POWER_OFF = "power-off"
MODE_GOOUT_ON = "goout-on"
MODE_GOOUT_OFF = "goout-off"
MODE_INSIDE_HEAT = "inside-heat"
MODE_ONDOL_HEAT = "ondol-heat"
MODE_ONLY_HOTWATER = "only-hotwater"
MODE_HOTWATER_TEMPERATURE = "hotwater-temperature"
MODE_HEAT_INTENSITY = "heat-intensity"
MODE_TIME_CYCLE_RESERVATION = "time-cycle-reservation"
MODE_DAY_CYCLE_RESERVATION = "day-cycle-reservation"
MODE_FASTDHW = "fastDHW"
MODE_SMART_FASTDHW = "smartFastDHW"
MODE_BOOST_HOTWATER = "boost-hotwater"

# 기능별 명령 표 — **봉투가 확정되면 그대로 쓴다.**
#
# 앱의 `BoilerConstants` + `ControlRequestMode` 조합이다. 실측으로 관측한
# 응답 command 값이 여기 그대로 있다 (0x02000001·02·04·05·06·0B).
#
# 값 범위는 반드시 서버 메타데이터로 검증한 뒤 보낼 것 (가스 기기다).
CONTROL_ACTIONS: dict[str, tuple[int, str, str | None]] = {
    # 이름            (command,              mode,                      값을 실을 상태 필드)
    "power_off": (CMD_POWER_OFF, MODE_POWER_OFF, None),
    "power_on": (CMD_POWER_ON, MODE_POWER_ON, None),
    "goout_on": (CMD_HOME_OUT_ON, MODE_GOOUT_ON, None),
    "goout_off": (CMD_HOME_OUT_OFF, MODE_GOOUT_OFF, None),
    "only_hotwater": (CMD_HOT_WATER_ONLY_ON, MODE_ONLY_HOTWATER, None),
    "inside_temperature": (
        CMD_INNER_HEAT_TEMP,
        MODE_INSIDE_HEAT,
        "insideTemperatureSetting",
    ),
    "ondol_temperature": (
        CMD_ONDOL_HEAT_TEMP,
        MODE_ONDOL_HEAT,
        "ondolTemperatureSetting",
    ),
    "hot_water_temperature": (
        CMD_HOT_WATER_TEMP,
        MODE_HOTWATER_TEMPERATURE,
        "hotWaterTemperatureSetting",
    ),
}

# 자발 보고 사유 (`0x04` 계열의 n).
REPORT_REASON_NAMES: dict[int, str] = {
    10: "실온 변화",
    4: "가동 상태 변화",
}

# `operationBusy` — 실측 확정. 온돌난방을 켜자 1→2 로 바뀌며 공급수온도가
# 31.5→44°C 로 올랐고, 끄자 2→1 로 돌아왔다.
BUSY_IDLE = 1
BUSY_RUNNING = 2
BUSY_NAMES: dict[int, str] = {BUSY_IDLE: "대기", BUSY_RUNNING: "가동중"}

# 상태 조회 명령. 앱 `BoilerConstants.STATUS_REQ` 와 같다.
COMMAND_STATUS = CMD_STATUS_REQ


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _half_degree(value: Any) -> float | None:
    """0.5°C 단위 정수 → °C. 확정된 스케일이다 (실측 교차검증)."""
    number = _as_int(value)
    return None if number is None else number / 2


def _tenth_degree(value: Any) -> float | None:
    """0.1°C 단위 정수 → °C."""
    number = _as_int(value)
    return None if number is None else number / 10


def _walk_strings(source: Any, key_names: tuple[str, ...], depth: int = 6) -> list[str]:
    """중첩 dict 에서 지정한 키의 문자열 값을 모은다.

    보일러 REST 원본의 정확한 구조를 아직 모른다(실측 1대뿐). `macAddress` 나
    `additionalValue` 가 어느 깊이에 있어도 식별자로 쓸 수 있게 훑는다.
    """
    found: list[str] = []
    if depth <= 0:
        return found
    if isinstance(source, dict):
        for key, value in source.items():
            if key in key_names and isinstance(value, (str, int)):
                text = str(value).strip()
                if text:
                    found.append(text)
            else:
                found.extend(_walk_strings(value, key_names, depth - 1))
    elif isinstance(source, list):
        for item in source:
            found.extend(_walk_strings(item, key_names, depth - 1))
    return found


@dataclass(slots=True)
class BoilerDevice:
    """보일러 하나. `status` 는 MQTT 응답이 올 때마다 갈린다."""

    device_seq: int
    device_id: str
    service_code: int
    model_code: str
    model_name: str
    nickname: str
    connected_registry: bool
    # MQTT 토픽 끝 조각(mac+additionalValue)과 대조할 식별자들. 전부 소문자.
    identifiers: frozenset[str]
    # 요청 봉투에 필요한 값. 앱은 `deviceId` 를 잘라 쓴다 (APK 확인) —
    # 앞 12자가 `macAddress`, 12~16자가 `additionalValue` 다.
    mac_address: str | None
    additional_value: str | None
    device_type: int | None
    # 응답 토픽에 들어가는 기기별 값. `cmd/{modelCode}/{mqttTopicKey}/mobile-{clientId}/res`
    # 형식이다 (APK 확인). 관측된 `242689/0/116659` 가 이 값이다.
    mqtt_topic_key: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)
    status: dict[str, Any] = field(repr=False, default_factory=dict)
    # 마지막으로 상태를 받은 시각(monotonic). 없으면 아직 한 번도 못 받은 것이다.
    status_stamp: float | None = field(repr=False, default=None)
    # 무엇이 언제 왔는지 짧게 남긴다. 개인정보는 없다 — 명령 코드와 모드뿐이다.
    state_log: list[dict[str, Any]] = field(repr=False, default_factory=list)

    # -- 생성 --------------------------------------------------------------

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> BoilerDevice | None:
        device_seq = raw.get("deviceSeq")
        service_code = _as_int(raw.get("serviceCode"))
        if device_seq is None or service_code is None:
            return None

        # **`deviceId` 가 `macAddress`(12자) + `additionalValue`(4자) 다** — 앱이
        # 그렇게 잘라 쓴다(APK `boilerMGPPMqttPayload`). 실측 토픽 끝 조각
        # `xxxxxxxxxxxx0000` 같은 16자 값이 곧 `deviceId` 다.
        device_id = str(raw.get("deviceId") or f"boiler-{device_seq}")
        macs = _walk_strings(raw, ("macAddress",))
        extras = _walk_strings(raw, ("additionalValue",))
        # `deviceId` 를 자른 값을 우선한다 — 앱과 같은 규칙이라야 서버가 받는다.
        if len(device_id) >= 16 and not device_id.startswith("boiler-"):
            macs = [device_id[:12], *macs]
            extras = [device_id[12:16], *extras]
        ids = {device_id.lower()}
        for mac in macs:
            ids.add(mac.lower())
            for extra in extras:
                ids.add(f"{mac}{extra}".lower())

        nickname_raw = None
        properties = raw.get("Properties")
        if isinstance(properties, dict):
            nick = properties.get("nickName")
            if isinstance(nick, dict):
                nickname_raw = nick.get("mainItem")
            elif isinstance(nick, str):
                nickname_raw = nick

        return cls(
            device_seq=int(device_seq),
            device_id=device_id,
            service_code=service_code,
            model_code=str(raw.get("modelCode") or ""),
            model_name=str(raw.get("modelName") or "나비엔 보일러"),
            nickname=str(nickname_raw or raw.get("modelName") or "나비엔 보일러"),
            # `connected` 키가 없으면 True 로 둔다 — 키 부재는 「모른다」이지
            # 「꺼져 있다」가 아니고, False 로 두면 엔티티가 영구히 사용 불가로 남는다.
            connected_registry=bool(raw.get("connected", True)),
            identifiers=frozenset(ids),
            mac_address=macs[0] if macs else None,
            additional_value=extras[0] if extras else None,
            device_type=_as_int(raw.get("deviceType")),
            mqtt_topic_key=(
                str(key) if (key := raw.get("mqttTopicKey")) else None
            ),
            raw=raw,
        )

    @property
    def client_id(self) -> str | None:
        """기기의 MQTT clientID (`roomcon-` + 콜론 없는 MAC).

        요청 토픽에 쓴다 — `cmd/{modelCode}/roomcon-{mac}/{status|control}`.
        **봉투의 `clientID` 필드에는 이 값이 아니라 `mobile-<우리 clientId>` 가
        들어간다** (APK 확인). 응답에 `roomcon-...` 이 찍히는 것은 기기가 자기를
        밝히는 것이다.
        """
        if not self.mac_address:
            return None
        return f"roomcon-{self.mac_address.lower()}"

    @property
    def feature(self) -> dict[str, Any]:
        """기기가 등록 시점에 올린 능력 메타데이터. 온도 범위가 여기 있다.

        서버가 `Properties.did` 를 주지 않으면(2026-10~) 모델별 대체값
        (`KNOWN_MODEL_FEATURES`)을 쓴다. 서버 선언이 있으면 항상 그쪽이 우선이다.
        """
        current: Any = self.raw
        for key in ("Properties", "did", "response", "feature"):
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if isinstance(current, dict) and current:
            return current
        return dict(KNOWN_MODEL_FEATURES.get(self.model_name, {}))

    @property
    def feature_is_fallback(self) -> bool:
        """`feature` 가 서버 선언이 아니라 대체값인가. 진단에서 가리려고 둔다."""
        current: Any = self.raw
        for key in ("Properties", "did", "response", "feature"):
            if not isinstance(current, dict):
                return self.model_name in KNOWN_MODEL_FEATURES
            current = current.get(key)
        if isinstance(current, dict) and current:
            return False
        return self.model_name in KNOWN_MODEL_FEATURES

    def temperature_range(self, prefix: str) -> tuple[float, float] | None:
        """`insideTemperature` 같은 접두사로 서버가 알려준 범위를 섭씨로 돌려준다.

        **없으면 `None`.** 추측한 범위로 가스 기기를 건드리지 않는다 —
        엔티티를 만들지 않는 쪽을 고른다.
        """
        feature = self.feature
        low = _as_int(feature.get(f"{prefix}Min"))
        high = _as_int(feature.get(f"{prefix}Max"))
        if low is None or high is None or low >= high:
            return None
        return low / 2, high / 2

    def supports(self, flag: str) -> bool:
        """`*Use` 플래그. **2 = 지원, 1 = 미지원** (해독 완료).

        키가 없으면 「모른다」이므로 `False` 로 둔다 — 없는 기능에 엔티티를
        만드는 것보다 낫다.
        """
        return _as_int(self.feature.get(flag)) == 2

    def request_topic(self, kind: str) -> str | None:
        """`cmd/{modelCode}/roomcon-{mac}/{kind}` — kind 는 `status` 또는 `control`."""
        client = self.client_id
        if client is None or not self.model_code:
            return None
        return f"cmd/{self.model_code}/{client}/{kind}"

    def response_topic(self, mqtt_client_id: str) -> str | None:
        """`cmd/{modelCode}/{mqttTopicKey}/mobile-{clientId}/res`.

        `mqttTopicKey` 가 없으면 만들 수 없다 — 그때는 서버가 응답을 어디로
        보낼지 알 수 없으므로 요청을 보내지 않는 편이 낫다.
        """
        if not self.mqtt_topic_key or not self.model_code or not mqtt_client_id:
            return None
        return (
            f"cmd/{self.model_code}/{self.mqtt_topic_key}"
            f"/mobile-{mqtt_client_id}/res"
        )

    def matches(self, device_key: str) -> bool:
        return bool(device_key) and device_key.lower() in self.identifiers

    # -- 상태 반영 ----------------------------------------------------------

    def apply_status(self, status: dict[str, Any]) -> None:
        """MQTT 응답의 `status` 를 통째로 바꾼다.

        매트의 `apply_reported` 는 부분 응답 때문에 겹쳐 쓰지만, 보일러 응답은
        실측에서 항상 전체 상태였다. 부분 응답이 확인되면 그때 겹쳐 쓰기로 바꾼다.
        """
        self.status = status
        self.status_stamp = time.monotonic()
        self.state_log.append(
            {
                "at": round(time.time()),
                "command": self.command_code,
                "operationMode": _as_int(status.get("operationMode")),
                "errorCode": _as_int(status.get("errorCode")),
            }
        )
        del self.state_log[:-_LOG_KEEP]

    # -- 읽기 ---------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.connected_registry

    @property
    def has_status(self) -> bool:
        return bool(self.status)

    @property
    def command_code(self) -> int | None:
        """`command` 의 n. 상위 바이트(출처)를 떼고 남은 값."""
        value = _as_int(self.status.get("command"))
        return None if value is None else value & 0xFFFFFF

    @property
    def command_source(self) -> int | None:
        """`command` 의 상위 바이트. 0x02 = 클라이언트 응답, 0x04 = 기기 자발 보고."""
        value = _as_int(self.status.get("command"))
        return None if value is None else value >> 24

    @property
    def is_device_report(self) -> bool:
        """이 상태가 기기가 스스로 올린 것인가."""
        return self.command_source == COMMAND_SOURCE_DEVICE

    @property
    def report_reason(self) -> str | None:
        """자발 보고 사유. 클라이언트 응답이면 `None`."""
        if not self.is_device_report:
            return None
        code = self.command_code
        if code is None:
            return None
        return REPORT_REASON_NAMES.get(code, f"알 수 없음({code})")

    @property
    def operation_busy(self) -> int | None:
        return _as_int(self.status.get("operationBusy"))

    @property
    def is_running(self) -> bool | None:
        """실제로 연소 중인가. 값을 모르면 `None` — 추측하지 않는다."""
        busy = self.operation_busy
        if busy not in (BUSY_IDLE, BUSY_RUNNING):
            return None
        return busy == BUSY_RUNNING

    @property
    def operation_mode(self) -> int | None:
        return _as_int(self.status.get("operationMode"))

    @property
    def operation_mode_name(self) -> str | None:
        """운전 모드 이름. 표에 없는 값은 숫자를 숨기지 않고 그대로 보여준다."""
        mode = self.operation_mode
        if mode is None:
            return None
        return OPERATION_MODE_NAMES.get(mode, f"알 수 없음({mode})")

    @property
    def error_code(self) -> int | None:
        return _as_int(self.status.get("errorCode"))

    @property
    def sub_error_code(self) -> int | None:
        return _as_int(self.status.get("subErrorCode"))

    @property
    def inside_temperature(self) -> float | None:
        """현재 실내 온도.

        `actualInsideTemperature`(0.1°C)가 더 정밀해 그쪽을 먼저 쓴다.
        없으면 `insideTemperature`(0.5°C)로 내려간다 — 둘 다 실측으로 확인됐다.
        """
        precise = _tenth_degree(self.status.get("actualInsideTemperature"))
        if precise is not None:
            return precise
        return _half_degree(self.status.get("insideTemperature"))

    @property
    def inside_temperature_setting(self) -> float | None:
        return _half_degree(self.status.get("insideTemperatureSetting"))

    @property
    def ondol_temperature_setting(self) -> float | None:
        return _half_degree(self.status.get("ondolTemperatureSetting"))

    @property
    def hot_water_temperature(self) -> float | None:
        return _half_degree(self.status.get("hotWaterTemperature"))

    @property
    def hot_water_temperature_setting(self) -> float | None:
        return _half_degree(self.status.get("hotWaterTemperatureSetting"))

    @property
    def supply_temperature(self) -> float | None:
        return _half_degree(self.status.get("supplyTemperature"))

    @property
    def return_temperature(self) -> float | None:
        return _half_degree(self.status.get("returnTemperature"))

    @property
    def outside_temperature(self) -> float | None:
        """외기 온도. **0.1°C 단위로 추정** — 실측 280→28.0°C 가 계절과 부합하지만
        교차검증할 두 번째 값이 아직 없다. 센서 속성에 추정임을 남긴다."""
        return _tenth_degree(self.status.get("outsideTemperature"))

    @property
    def wifi_rssi(self) -> int | None:
        """Wi-Fi 신호. 실측 45 — 부호 없는 dBm 크기로 보이나 확인 전이라 그대로 둔다."""
        return _as_int(self.status.get("wifiRssi"))

    def as_diagnostics(self) -> dict[str, Any]:
        """진단 내보내기용 요약. 개인정보 없음 — 상태 값과 기록뿐이다."""
        return {
            "has_status": self.has_status,
            "status_age": (
                None
                if self.status_stamp is None
                else round(time.monotonic() - self.status_stamp, 1)
            ),
            "feature_is_fallback": self.feature_is_fallback,
            "state_log": list(self.state_log),
        }
