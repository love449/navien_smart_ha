"""온도형 매트의 온도조절기 (`heatControl.unit == "0.5C"`).

온도형은 `temperature.current` 를 함께 보내주므로 `climate` 가 맞다 — 서모스탯
카드의 현재 온도 칸이 채워진다.

단계형(`1.0L`)은 이 플랫폼을 만들지 않는다. 현재값이 오지 않아 카드 절반이 비고,
단계를 도(°)로 표시하면 사용자가 오해한다. 그쪽은 `number` 슬라이더를 쓴다.

**주의 — 온도형 매트를 직접 눌러본 적은 없다.** 집에 단계형만 있다. 사계절
모델(EMF520)의 난방·냉방 표시와 제어는 **제보로 확인됐다.**
구조는 앱 코드로 확정했으나, 특히 `enable: false` 전송은 확인하지 못했다.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.climate import ATTR_PRESET_MODE
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.restore_state import RestoreEntity

from homeassistant.exceptions import HomeAssistantError

from . import NavienSmartConfigEntry
from .boiler import BoilerDevice
from .const import MODE_HEAT, ZONE_NAMES
from .coordinator import NavienSmartCoordinator
from .entity import BoilerEntity, NavienSmartEntity
from .models import NavienDevice

_LOGGER = logging.getLogger(__name__)

# 보일러 운전 preset. **기기가 배타적으로 하나만 켠다** — 외출·실내난방·온돌난방이
# 겹쳐 동작할 수 없으므로 한 엔티티에서 고르게 한다.
_PRESET_INSIDE = "실내난방"
_PRESET_ONDOL = "온돌난방"
_PRESET_AWAY = "외출(온수전용)"

# 보일러 온도 **연속 입력 묶음** (2026-10-29).
#
# 서모스탯 카드의 +/- 를 연달아 누르면 누를 때마다 명령이 하나씩 나가고, 기기
# 응답이 2~5초씩 늦게 도착해 화면 숫자가 「내렸는데 다시 올라가는」 식으로 출렁였다.
# 마지막 입력 뒤 `_DEBOUNCE_SECONDS` 동안 추가 입력이 없을 때 **마지막 값 하나만**
# 보낸다. 보낸 뒤에는 기기가 그 값을 확인해 줄 때까지(최대 `_PENDING_HOLD_SECONDS`)
# 화면에 누른 값을 유지한다 — 시간이 지나면 기기가 알려준 값으로 돌아간다.
_DEBOUNCE_SECONDS = 1.5
_PENDING_HOLD_SECONDS = 15.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavienSmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[NavienSmartThermostat] = []
    for device in (coordinator.data or {}).values():
        control = device.heat_control
        if control is None or not control.is_celsius:
            continue
        entities.extend(
            NavienSmartThermostat(coordinator, device, zone) for zone in device.zones
        )
    # 보일러 난방. 실내·온돌 중 **하나라도** 지원하고 범위를 알려준 경우만 만든다.
    boiler_entities: list[ClimateEntity] = []
    for boiler in coordinator.boiler.values():
        if BoilerThermostat.presets_for(boiler):
            boiler_entities.append(BoilerThermostat(coordinator, boiler))
    async_add_entities([*entities, *boiler_entities])


def _raise_if_failed(outcome: dict[str, Any], prefix: str) -> None:
    """보내지 못한 기기가 있으면 이유를 담아 올린다."""
    reasons = [
        str(item.get("reason"))
        for item in outcome.get("results") or []
        if not item.get("sent")
    ]
    if reasons:
        raise HomeAssistantError(f"{prefix}: {'; '.join(reasons)}")


class NavienSmartThermostat(NavienSmartEntity, ClimateEntity):
    """구역 하나의 난방 온도."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        coordinator: NavienSmartCoordinator,
        device: NavienDevice,
        zone: str,
    ) -> None:
        super().__init__(coordinator, device)
        self._zone = zone
        self._attr_unique_id = f"{device.device_id}_{zone}_thermostat"

        label = device.zone_names.get(zone) or ZONE_NAMES.get(zone, zone)
        self._attr_name = label if device.is_double else "난방"

        # 범위를 `__init__` 에 고정하지 않는다. 사계절 모델은 사용자가 앱에서
        # WARM/COOL 을 바꾸면 **범위 자체가 갈린다** (난방 28~45 / 냉방 20~35).
        # 고정해두면 냉방 설정값이 자기 최소값보다 낮아 카드가 깨진다.
        assert device.heat_control is not None  # setup 에서 걸러진다

    @property
    def _control(self) -> Any:
        """지금 적용되는 제어 서술자. 냉방이면 `coolControl`."""
        device = self.device
        return device.active_control if device is not None else None

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """냉방 중에는 냉방만, 그 밖에는 난방만 보여준다.

        `season`(WARM/COOL) 은 **앱에서 고르는 모드**이고 우리가 바꾸는 방법을
        확인하지 못했다. 그래서 HA 에서 난방↔냉방 전환을 제공하지 않는다 —
        고를 수 있는 것처럼 보이면 눌렀을 때 아무 일도 안 일어난다.
        """
        device = self.device
        cooling = device is not None and device.is_cooling
        return [HVACMode.OFF, HVACMode.COOL if cooling else HVACMode.HEAT]

    @property
    def min_temp(self) -> float:
        control = self._control
        return float((control.range_min if control else None) or 20)

    @property
    def max_temp(self) -> float:
        control = self._control
        return float((control.range_max if control else None) or 45)

    @property
    def target_temperature_step(self) -> float:
        control = self._control
        return control.step if control else 0.5

    @property
    def available(self) -> bool:
        """냉방 중에도 쓴다.

        v0.9.0 까지는 냉방이면 손을 뗐다 — 값 체계를 몰랐기 때문이다. 이제
        `coolControl`(범위·간격·고온경고선)이 서버에서 오고, 설정값이 난방과 같은
        `heater.<구역>.temperature.set` 으로 오는 것을 실기기 제보로 관측했다.

        다만 `season` 이 **`SEASON_SUMMER`(2)** 일 때만 냉방으로 다룬다. 값이
        없거나 모르는 값이면 난방으로 두므로, 잘못된 범위를 쓸 일이 없다.
        """
        return super().available

    @property
    def current_temperature(self) -> float | None:
        device = self.device
        return None if device is None else device.zone_current(self._zone)

    @property
    def target_temperature(self) -> float | None:
        device = self.device
        return None if device is None else device.zone_setting(self._zone)

    @property
    def hvac_mode(self) -> HVACMode | None:
        """기기 전원과 구역 `enable` 을 함께 본다.

        기기가 꺼져 있으면 구역이 켜져 있어도 난방하지 않는다.
        """
        device = self.device
        if device is None:
            return None
        # **모르는 것을 「꺼짐」이라 하지 않는다.** `is_on` 은 `operationMode` 가
        # 없을 때 `False` 를 돌려주는데, 그것을 그대로 쓰면 상태가 아직 안 온
        # 기기를 껐다고 단정한다 — 사계절 제보에서 좌우 모두 「꺼짐」으로 보인
        # 원인이다.
        if device.operation_mode is None:
            return None
        if not device.is_on:
            return HVACMode.OFF
        enabled = device.zone_enabled(self._zone)
        running = HVACMode.COOL if device.is_cooling else HVACMode.HEAT
        return running if enabled is not False else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        mode = self.hvac_mode
        if mode is None:
            return None
        if mode is HVACMode.HEAT:
            return HVACAction.HEATING
        if mode is HVACMode.COOL:
            return HVACAction.COOLING
        return HVACAction.OFF

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        attrs: dict[str, Any] = {"zone": self._zone}
        control = device.heat_control
        if control is not None and control.safe_value is not None:
            # 고온경고 기준선. 상한이 아니다.
            attrs["high_temp_warning_temperature"] = control.safe_value
        if device.is_four_season:
            attrs["four_season"] = True
            attrs["season"] = device.season
        return attrs

    @property
    def _target_zones(self) -> tuple[str, ...]:
        """이 조작이 적용될 구역 — **언제나 이 구역 하나다.**

        v0.9.0~v0.11.0 은 냉방+좌우분리면 두 구역에 같은 값을 보냈다. 근거는 앱
        안내문이었다.

            COOL 모드 — 매트의 좌우가 같은 온도로 동작합니다

        **모델 얘기를 빠뜨린 문구였다.** 나비엔 제품 페이지는 이렇게 적는다.

            0.5℃ 분리 냉난방 기술로 좌우 원하는 온도로
            해당 기능은 **사계절형 Pro 모델에만** 적용됩니다

        즉 Pro 는 냉방에서도 좌우가 따로 가고 Air 는 같이 간다. **모델별 동작을
        코드에 박아넣은 셈이었고**, 그건 이 통합이 하지 않기로 한 것이다.
        서버가 Pro/Air 를 알려주지도 않는다.

        그래서 **누르신 구역에만 보낸다.** 기기가 좌우를 묶어 도는 모델이면
        응답으로 두 값을 같게 돌려줄 것이고, 우리는 그것을 그대로 보여준다.
        **기기가 하는 일을 앞질러 정하지 않는다.**
        """
        return (self._zone,)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get("temperature")
        device = self.device
        if device is None or temperature is None:
            return
        value = float(temperature)
        zones = self._target_zones
        heater = device.build_heater_desired(
            changes={zone: value for zone in zones},
            enables={zone: True for zone in zones},
        )
        await self.coordinator.async_send(
            device, {"operationMode": MODE_HEAT, "heater": heater}
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        device = self.device
        if device is None:
            return
        zones = self._target_zones
        if hvac_mode is not HVACMode.OFF:
            # 난방이든 냉방이든 켜는 방법은 같다 — `operationMode` 1 + 구역 `enable`.
            # 난방·냉방을 가르는 것은 `season` 이고 그건 앱에서 고른다.
            heater = device.build_heater_desired(
                enables={zone: True for zone in zones}
            )
            await self.coordinator.async_send(
                device, {"operationMode": MODE_HEAT, "heater": heater}
            )
            return
        # 구역만 끈다. 기기 전원은 별도 스위치가 담당한다.
        heater = device.build_heater_desired(
            enables={zone: False for zone in zones}
        )
        await self.coordinator.async_send(device, {"heater": heater})


class BoilerThermostat(BoilerEntity, ClimateEntity, RestoreEntity):
    """보일러 난방 (실내 / 온돌).

    **기기는 난방 방식을 하나만 켠다.** 실내난방과 온돌난방이 배타적이고 각자
    다른 설정 온도를 가진다(실내 10~40°C, 온돌 30~65°C). 그래서 `climate` 를
    둘로 나누지 않고 하나에 `preset` 으로 담는다 — 둘로 나누면 「양쪽 다 켜짐」
    이라는 불가능한 상태를 표현할 수 있게 되어 오히려 헷갈린다.

    끄기(`OFF`)는 **전원을 끄지 않는다.** `only-hotwater` 를 보내 난방만 쉬고
    온수는 유지한다 — 룸콘의 「온수전용」이 그것이다. 전원 자체를 끄는 것은
    전원 스위치가 맡는다.

    모드 전환과 온도 설정이 **같은 명령**이다 (`INNER_HEAT_TEMP_SETTING_REQ` 등).
    그래서 preset 을 바꿀 때 그 모드의 현재 설정 온도를 함께 보낸다.
    """

    _attr_name = "난방"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_target_temperature_step = 0.5
    _attr_precision = 0.5

    # preset → (전환 동작, 온도 동작, 상태 필드, 범위 접두사, operationMode 값)
    #
    # **전환과 온도가 다른 preset 이 있다.** 실내·온돌은 모드 전환 명령이 곧 온도
    # 설정 명령이지만(같은 `command`), 외출은 전환이 `goout-on` 이고 온도는 온수
    # 명령이다 — 외출 중에는 난방을 쉬고 온수만 쓰므로 조절할 것이 그것뿐이다.
    _PRESETS: dict[str, tuple[str, str, str, str, int]] = {
        _PRESET_INSIDE: (
            "inside_temperature",
            "inside_temperature",
            "insideTemperatureSetting",
            "insideTemperature",
            5,
        ),
        _PRESET_ONDOL: (
            "ondol_temperature",
            "ondol_temperature",
            "ondolTemperatureSetting",
            "ondolTemperature",
            6,
        ),
        _PRESET_AWAY: (
            # 실측으로 확인된 전환 명령이다 (0x02000004 → operationMode 4).
            "goout_on",
            "hot_water_temperature",
            "hotWaterTemperatureSetting",
            "hotWaterTemperature",
            4,
        ),
    }

    @staticmethod
    def presets_for(device: BoilerDevice) -> list[str]:
        """이 기기가 쓸 수 있는 난방 preset. 범위를 모르면 넣지 않는다."""
        available: list[str] = []
        if device.supports("insideUse") and device.temperature_range(
            "insideTemperature"
        ):
            available.append(_PRESET_INSIDE)
        if device.supports("ondolUse") and device.temperature_range("ondolTemperature"):
            available.append(_PRESET_ONDOL)
        # 외출은 온수 온도를 조절하므로 온수 범위가 필요하다.
        if device.supports("gooutUse") and device.temperature_range(
            "hotWaterTemperature"
        ):
            available.append(_PRESET_AWAY)
        return available

    def __init__(
        self, coordinator: NavienSmartCoordinator, device: BoilerDevice
    ) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_climate"
        self._attr_preset_modes = self.presets_for(device)
        # 난방을 켜라고만 했을 때 무엇으로 켤지. 지원하는 첫 번째를 쓴다.
        self._default_preset = self._attr_preset_modes[0]
        # **꺼져 있을 때 보여줄 값.** 마지막으로 켜져 있던 preset 을 기억한다 —
        # 전원을 끌 때마다 「실내난방」으로 되돌아가면 온돌을 쓰는 집에서는
        # 매번 다시 골라야 한다. 아래에서 갱신하고, 재시작 시 복원한다.
        self._last_preset = self._default_preset
        # 연속 입력 묶음 (`_DEBOUNCE_SECONDS` 주석). (preset, °C) — 아직 기기가
        # 확인하지 않은 마지막 입력값이다.
        self._pending: tuple[str, float] | None = None
        self._pending_until = 0.0
        self._flush_unsub: CALLBACK_TYPE | None = None

    async def async_added_to_hass(self) -> None:
        """재시작 후 마지막 preset 을 되살린다.

        **잠정값이다.** 기기가 상태를 올리면 실제 모드로 덮인다. 아무것도 없는
        것보다 마지막으로 알던 것이 낫다 — 에어원 상태 복원과 같은 판단이다.
        """
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is None:
            return
        stored = last.attributes.get(ATTR_PRESET_MODE)
        if stored in (self._attr_preset_modes or ()):
            self._last_preset = stored

    async def async_will_remove_from_hass(self) -> None:
        """예약해 둔 온도 전송을 취소한다 — 통합을 내릴 때 뒤늦게 나가지 않게."""
        self._drop_pending()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """상태가 올 때마다 **켜져 있는 preset 을 기억한다.**

        `preset_mode` 프로퍼티에서 기록하지 않는다 — 값을 읽는 곳이 상태를
        바꾸면 HA 가 언제 몇 번 읽는지에 따라 결과가 달라진다.

        기다리던 온도를 기기가 확인해 주면 대기값을 지운다.
        """
        device = self.device
        if device is not None and device.has_status:
            mode = device.operation_mode
            for name, (_sw, _tmp, _f, _p, value) in self._PRESETS.items():
                if mode == value and name in (self._attr_preset_modes or ()):
                    self._last_preset = name
                    break
        if self._pending is not None and self._flush_unsub is None:
            preset, wanted = self._pending
            _sw, _tmp, field, _prefix, mode_value = self._PRESETS[preset]
            confirmed = False
            if device is not None and device.has_status:
                raw = device.status.get(field)
                confirmed = (
                    device.status.get("operationMode") == mode_value
                    and raw is not None
                    and int(raw) == round(wanted * 2)
                )
            if confirmed or time.monotonic() > self._pending_until:
                self._pending = None
        super()._handle_coordinator_update()

    # -- 지금 무엇을 하고 있는가 -------------------------------------------

    @property
    def preset_mode(self) -> str | None:
        """지금 켜져 있는 운전 방식.

        꺼져 있으면 **마지막으로 켜져 있던 것**을 보여준다. 그 값이 목표 온도와
        범위도 함께 정하므로, 카드가 마지막으로 쓰던 모습 그대로 남는다.
        """
        device = self.device
        if device is None or not device.has_status:
            return None
        mode = device.operation_mode
        for name, (_sw, _tmp, _field, _prefix, value) in self._PRESETS.items():
            if mode == value:
                return name
        return self._last_preset

    @property
    def hvac_mode(self) -> HVACMode | None:
        device = self.device
        if device is None or not device.has_status:
            return None
        mode = device.operation_mode
        if mode is None:
            return None
        # 외출(4)도 전원이 켜진 상태다 — preset 으로 구분한다.
        return HVACMode.OFF if mode == 1 else HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction | None:
        """실제로 연소 중인가. 설정 온도에 닿으면 모드는 그대로여도 멈춘다."""
        device = self.device
        if device is None or not device.has_status:
            return None
        if self.hvac_mode is not HVACMode.HEAT:
            return HVACAction.OFF
        # **외출 중에는 난방을 하지 않는다.** `operationBusy` 가 2 여도 그것은
        # 온수를 데우는 것이므로 난방 동작으로 보여주지 않는다.
        if device.operation_mode == 4:
            return HVACAction.IDLE
        running = device.is_running
        if running is None:
            return None
        return HVACAction.HEATING if running else HVACAction.IDLE

    @property
    def current_temperature(self) -> float | None:
        """현재 실온. **온돌난방일 때도 실온을 보여준다** — 기기가 그것만 준다."""
        device = self.device
        if device is None or not device.has_status:
            return None
        return device.inside_temperature

    @property
    def target_temperature(self) -> float | None:
        device = self.device
        preset = self.preset_mode
        if device is None or not device.has_status or preset is None:
            return None
        # 아직 기기가 확인하지 않은 마지막 입력이 있으면 그것을 보여준다 —
        # 응답이 늦게 와서 숫자가 출렁이는 것을 막는다. 시간이 지나면 기기 값으로.
        if (
            self._pending is not None
            and self._pending[0] == preset
            and time.monotonic() <= self._pending_until
        ):
            return self._pending[1]
        _sw, _tmp, field, _prefix, _value = self._PRESETS[preset]
        raw = device.status.get(field)
        return None if raw is None else int(raw) / 2

    @property
    def min_temp(self) -> float:
        return self._bounds()[0]

    @property
    def max_temp(self) -> float:
        return self._bounds()[1]

    def _bounds(self) -> tuple[float, float]:
        """**preset 에 따라 범위가 바뀐다** (실내 10~40, 온돌 30~65).

        기기가 없거나 범위를 모르면 HA 기본값을 쓴다 — 그 상태에서는 전송이
        coordinator 에서 막히므로 잘못된 값이 나갈 일은 없다.
        """
        device = self.device
        preset = self.preset_mode
        if device is not None and preset is not None:
            _sw, _tmp, _field, prefix, _value = self._PRESETS[preset]
            if (bounds := device.temperature_range(prefix)) is not None:
                return bounds
        return 5.0, 35.0

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None or not device.has_status:
            return None
        attrs: dict[str, Any] = {}
        if device.operation_mode_name is not None:
            attrs["boiler_mode"] = device.operation_mode_name
        # 난방수 온도. 온돌난방에서 실제로 도는 값이라 진단에 유용하다.
        if device.supply_temperature is not None:
            attrs["supply_temperature"] = device.supply_temperature
        if device.return_temperature is not None:
            attrs["return_temperature"] = device.return_temperature
        if self._pending is not None:
            # 보냈거나 보낼 예정이지만 기기가 아직 확인하지 않은 값.
            attrs["pending_temperature"] = self._pending[1]
        return attrs or None

    # -- 조작 ---------------------------------------------------------------

    @callback
    def _drop_pending(self) -> None:
        """대기 중인 온도 입력을 버린다 (예약된 전송도 취소)."""
        if self._flush_unsub is not None:
            self._flush_unsub()
            self._flush_unsub = None
        self._pending = None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """`OFF` 는 **전원을 끈다.**

        v0 에서는 `only-hotwater` 를 보내 난방만 쉬게 했다. 외출이 preset 으로
        들어오면서 그것과 구분이 사라졌다 — 「난방만 끄기」는 이제 preset 을
        외출로 바꾸는 것이다. `OFF` 는 문자 그대로 전원 끄기로 둔다.

        **대기 중인 온도 입력은 버린다.** 뒤늦게 나간 온도 명령이 방금 고른
        모드를 되돌리면 안 된다 (실내·온돌은 온도 명령이 곧 모드 전환이다).
        """
        self._drop_pending()
        if hvac_mode is HVACMode.OFF:
            outcome = await self.coordinator.async_boiler_request("power_off")
            _raise_if_failed(outcome, "보일러를 끄지 못했습니다")
            return
        await self._async_apply(self.preset_mode or self._default_preset, None)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in self._PRESETS:
            raise HomeAssistantError(f"모르는 운전 방식: {preset_mode}")
        # `async_set_hvac_mode` 와 같은 이유로 대기 중인 온도를 버린다.
        self._drop_pending()
        await self._async_apply(preset_mode, None)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """온도는 **바로 보내지 않고 묶는다** (`_DEBOUNCE_SECONDS` 주석).

        마지막 입력 뒤 잠깐 기다렸다가 그 값 하나만 보낸다. 그동안 또 누르면
        기다림을 다시 시작한다.
        """
        target = kwargs.get(ATTR_TEMPERATURE)
        if target is None:
            return
        preset = (
            kwargs.get(ATTR_PRESET_MODE) or self.preset_mode or self._default_preset
        )
        if preset not in self._PRESETS:
            raise HomeAssistantError(f"모르는 운전 방식: {preset}")
        if self._flush_unsub is not None:
            self._flush_unsub()
            self._flush_unsub = None
        self._pending = (preset, float(target))
        self._pending_until = (
            time.monotonic() + _DEBOUNCE_SECONDS + _PENDING_HOLD_SECONDS
        )
        self._flush_unsub = async_call_later(
            self.hass, _DEBOUNCE_SECONDS, self._async_flush_pending
        )
        self.async_write_ha_state()

    async def _async_flush_pending(self, _now: Any) -> None:
        """묶어 둔 마지막 온도를 보낸다.

        서비스 호출은 이미 끝났으므로 실패를 화면 오류로 올릴 수 없다 — 로그에
        남기고 대기값을 지워 기기 값으로 돌아가게 한다.
        """
        self._flush_unsub = None
        if self._pending is None:
            return
        preset, value = self._pending
        # 기기 확인을 기다리는 시간은 실제로 보낸 시점부터 센다.
        self._pending_until = time.monotonic() + _PENDING_HOLD_SECONDS
        try:
            await self._async_apply(preset, value)
        except HomeAssistantError as err:
            _LOGGER.warning(
                "보일러 %s 온도 %.1f°C 를 보내지 못했습니다: %s", preset, value, err
            )
            if self._pending == (preset, value):
                self._pending = None
            self.async_write_ha_state()

    async def _async_apply(self, preset: str, temperature: float | None) -> None:
        """preset 전환과 온도 설정을 보낸다.

        실내·온돌은 **모드 전환 명령이 곧 온도 설정 명령**이라 한 번에 끝난다.
        **외출은 둘이 다르다** — 전환은 `goout-on`, 온도는 온수 명령이다.

        `temperature` 가 `None` 이면 모드만 바꾼다. 이미 그 모드면 아무것도
        보내지 않는다 — 같은 명령을 헛되이 쏘지 않는다.
        """
        device = self.device
        if device is None:
            return
        switch_action, temp_action, field, _prefix, mode_value = self._PRESETS[preset]
        already = device.status.get("operationMode") == mode_value

        if temperature is None:
            if already:
                return
            if switch_action == temp_action:
                # 전환 명령이 온도를 요구한다. 그 모드의 현재 설정값을 그대로 실어
                # 보낸다 — 모드만 바꾸고 온도는 건드리지 않는 셈이다.
                outcome = await self.coordinator.async_boiler_request(
                    switch_action, value=self._current_setting(device, field, preset)
                )
            else:
                outcome = await self.coordinator.async_boiler_request(switch_action)
            _raise_if_failed(outcome, f"{preset} 으로 바꾸지 못했습니다")
            return

        # 온도를 바꾼다. 다른 모드였고 전환 명령이 따로면 먼저 전환한다.
        if not already and switch_action != temp_action:
            outcome = await self.coordinator.async_boiler_request(switch_action)
            _raise_if_failed(outcome, f"{preset} 으로 바꾸지 못했습니다")

        outcome = await self.coordinator.async_boiler_request(
            temp_action, value=temperature
        )
        _raise_if_failed(
            outcome, f"{preset} 온도를 {temperature}°C 로 바꾸지 못했습니다"
        )

    @staticmethod
    def _current_setting(device: BoilerDevice, field: str, preset: str) -> float:
        raw = device.status.get(field)
        if raw is None:
            raise HomeAssistantError(
                f"{preset} 의 현재 설정 온도를 알지 못해 보낼 수 없습니다. "
                "상태가 채워진 뒤 다시 시도해 주세요"
            )
        return int(raw) / 2
