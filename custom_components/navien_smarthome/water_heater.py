"""보일러 온수 온도.

**온수부터 만든 이유.** 난방과 독립적이라 여름에도 검증할 수 있고, 잘못 보내도
난방 동작에 영향이 없다. 봉투와 명령 코드는 실기기로 확인했다
(2026-08-04 07:49, `command=0x0200000B`).

범위는 **서버가 알려준 값만** 쓴다 (`hotWaterTemperatureMin/Max`). 메타데이터가
없으면 엔티티를 만들지 않는다 — 추측한 범위로 가스 기기를 건드리지 않는다.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.water_heater import (
    STATE_GAS,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import NavienSmartConfigEntry
from .boiler import BoilerDevice
from .coordinator import NavienSmartCoordinator
from .entity import BoilerEntity

# 서버 메타데이터의 접두사.
_RANGE_PREFIX = "hotWaterTemperature"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NavienSmartConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[WaterHeaterEntity] = []
    for boiler in coordinator.boiler.values():
        # 기기가 온수 온도 설정을 지원한다고 밝혔고, 범위도 알려준 경우만 만든다.
        if not boiler.supports("hotWaterTemperatureSettingUse"):
            continue
        if boiler.temperature_range(_RANGE_PREFIX) is None:
            continue
        entities.append(BoilerWaterHeater(coordinator, boiler))
    async_add_entities(entities)


class BoilerWaterHeater(BoilerEntity, WaterHeaterEntity):
    """온수 설정 온도.

    운전 모드는 건드리지 않는다 — 온수 온도만 바꾼다. 모드 전환은 `switch` ·
    `climate` 가 맡는다. 앱도 온수 온도를 모드와 독립적으로 조절한다.
    """

    _attr_name = "온수"
    _attr_icon = "mdi:water-thermometer"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = WaterHeaterEntityFeature.TARGET_TEMPERATURE
    _attr_precision = 0.5
    # **`water_heater` 의 상태값은 `current_operation` 이다.**
    #
    # 그것을 만들지 않으면 온도가 잘 보이는데도 상태가 「알 수 없음」으로 남는다
    # (`WaterHeaterEntity.state` 가 `current_operation` 을 그대로 돌려준다).
    #
    # 운전 방식을 고르게 하지는 않는다 — `OPERATION_MODE` 기능을 켜지 않았으므로
    # 카드에 드롭다운이 생기지 않고 표시만 된다. 이 보일러에서 온수는 난방 모드에
    # 딸린 것이라, 온수만 따로 켜고 끄는 명령이 없다.
    _attr_operation_list = [STATE_OFF, STATE_GAS]

    def __init__(
        self, coordinator: NavienSmartCoordinator, device: BoilerDevice
    ) -> None:
        super().__init__(coordinator, device)
        self._attr_unique_id = f"{device.device_id}_water_heater"
        bounds = device.temperature_range(_RANGE_PREFIX)
        # 위에서 `None` 을 걸렀으므로 여기서는 값이 있다.
        assert bounds is not None
        self._attr_min_temp, self._attr_max_temp = bounds
        # HA 가 슬라이더 간격에 쓴다. 기기가 0.5°C 단위다.
        self._attr_target_temperature_step = 0.5

    @property
    def current_operation(self) -> str | None:
        """카드에 보이는 상태. 전원이 꺼져 있으면 `off`, 켜져 있으면 `gas`.

        운전 모드(실내난방·온돌난방·외출)와 무관하게 **온수는 전원이 켜져 있으면
        쓸 수 있다** — 어느 모드에서든 온수 온도를 조절할 수 있는 것과 같은 이유다.
        """
        device = self.device
        if device is None or not device.has_status:
            return None
        mode = device.operation_mode
        if mode is None:
            return None
        return STATE_OFF if mode == 1 else STATE_GAS

    @property
    def current_temperature(self) -> float | None:
        """지금 배관의 온수 온도. **설정값이 아니다.**"""
        device = self.device
        if device is None or not device.has_status:
            return None
        return device.hot_water_temperature

    @property
    def target_temperature(self) -> float | None:
        device = self.device
        if device is None or not device.has_status:
            return None
        return device.hot_water_temperature_setting

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None
        attrs: dict[str, Any] = {}
        if device.operation_mode_name is not None:
            attrs["boiler_mode"] = device.operation_mode_name
        # 온수를 실제로 흘리고 있는지. 0 이면 안 쓰는 중이다.
        flow = device.status.get("DHWInternalFlowRate")
        if flow is not None:
            attrs["dhw_flow_rate_raw"] = flow
        return attrs or None

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """온수 설정 온도를 바꾼다.

        범위 검증은 coordinator 가 **서버 메타데이터로** 한 번 더 한다.
        여기서 막지 못한 값이 있어도 전송 전에 걸린다.
        """
        target = kwargs.get(ATTR_TEMPERATURE)
        if target is None:
            return
        outcome = await self.coordinator.async_boiler_request(
            action="hot_water_temperature", value=float(target)
        )
        _raise_if_failed(outcome, f"온수 온도를 {target}°C 로 바꾸지 못했습니다")
        # **낙관적 갱신을 하지 않는다.** 기기가 응답을 올리면 그때 바뀐다 —
        # 매트·에어원과 같은 원칙이다. 거부됐는데 UI 가 먼저 바뀌면 사용자는
        # 「됐다가 되돌아간다」를 겪는다.


def _raise_if_failed(outcome: dict[str, Any], prefix: str) -> None:
    """보내지 못한 기기가 있으면 이유를 담아 올린다.

    조용히 삼키면 사용자는 「눌렀는데 아무 일도 안 난다」만 겪는다.
    """
    reasons = [
        str(item.get("reason"))
        for item in outcome.get("results") or []
        if not item.get("sent")
    ]
    if reasons:
        raise HomeAssistantError(f"{prefix}: {'; '.join(reasons)}")
