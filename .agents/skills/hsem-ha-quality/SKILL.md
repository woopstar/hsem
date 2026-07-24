---
name: hsem-ha-quality
description: Activate when writing or reviewing Home Assistant integration code to ensure Bronze and Silver quality tier standards — HA constants, entity patterns, coordinator usage, config flow, and async correctness.
---

# HSEM Home Assistant Quality — Bronze & Silver Tier Standards

Activate this skill when writing or reviewing any code that touches Home Assistant integration surfaces. It enforces Bronze and Silver quality tier requirements from the [HA integration quality scale](https://developers.home-assistant.io/docs/quality_scale/).

## Bronze Tier — Must Have (Non-Negotiable)

These are required for any HA integration to function correctly.

### Constants — Never Hardcode

```python
# ✅ Use HA constants from homeassistant.const
from homeassistant.const import (
    CONF_NAME,
    PERCENTAGE,
    STATE_ON,
    STATE_OFF,
    UnitOfEnergy,
    Platform,
)

# ❌ Never hardcode strings
state = "on"           # use STATE_ON
unit = "kWh"           # use UnitOfEnergy.KILO_WATT_HOUR
unit = "%"             # use PERCENTAGE
```

Always check [`homeassistant/const.py`](https://github.com/home-assistant/core/blob/dev/homeassistant/const.py) before defining your own constant.

### Entity Pattern — Correct MRO

```python
# ✅ Correct: mixins BEFORE base class
class HSEMBatterySensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    ...

# ❌ Wrong: bare Entity, wrong order
class HSEMBatterySensor(SensorEntity, CoordinatorEntity):  # MRO violation
    ...
class HSEMBatterySensor(Entity):  # never use bare Entity
    ...
```

### Device Info & Unique ID — Every Entity

```python
# ✅ Every entity MUST have both
@property
def unique_id(self) -> str:
    return f"{self._entry.entry_id}_{self.entity_description.key}"

@property
def device_info(self) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, self._entry.entry_id)},
        name="HSEM",
        manufacturer="HSEM",
        model="Energy Management",
    )
```

### Config Flow — Every Step Returns Proper Dict

```python
# ✅ Every async_step_* returns a dict
async def async_step_user(self, user_input=None):
    if user_input is not None:
        return self.async_create_entry(title="HSEM", data=user_input)
    return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA)

# ❌ State leaks between steps — never store mutable state on self
```

### Manifest — Pinned Dependencies

```json
{
  "requirements": ["some-lib==1.2.3"],
  "dependencies": [],
  "codeowners": ["@owner"]
}
```
- Pin to exact versions: `"pkg==1.2.3"`
- `REQUIREMENTS` constant is deprecated — use `manifest.json` only
- No new third-party deps without justification

## Silver Tier — Should Have

### Async Correctness

```python
# ✅ Offload blocking calls
result = await self.hass.async_add_executor_job(cpu_intensive_fn)

# ✅ Error handling on tasks
try:
    self._task = hass.async_create_task(self._poll())
except Exception:
    _LOGGER.exception("Poll failed")

# ❌ Blocking on event loop
time.sleep(1)           # use asyncio.sleep
requests.get(url)       # use aiohttp / hass.helpers.aiohttp_client
open("file")            # use aiofiles or executor_job
```

### Coordinator Pattern

```python
# ✅ Use DataUpdateCoordinator for periodic/shared polling
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    CoordinatorEntity,
)

class HSEMCoordinator(DataUpdateCoordinator):
    async def _async_update_data(self):
        """Fetch latest data."""
        ...

# Entities extend CoordinatorEntity to get auto-updates
class HSEMSensor(CoordinatorEntity, SensorEntity):
    @property
    def native_value(self):
        return self.coordinator.data.get("my_value")
```

### Translations — Every User-Facing String

Every string the user sees must be in `translations/en.json`:
- Config flow step titles, descriptions, field labels
- Error messages (`error`, `abort`)
- Options flow fields
- Entity names in `entity` section

```json
{
  "config": {
    "step": {
      "user": {
        "title": "HSEM Setup",
        "data": {
          "name": "Integration Name"
        }
      }
    },
    "error": {
      "invalid_config": "Invalid configuration"
    },
    "abort": {
      "already_configured": "Already configured"
    }
  }
}
```

### Unload & Cleanup

```python
# ✅ Register in setup, remove in unload
async def async_setup_entry(hass, entry):
    entry.async_on_unload(entry.add_update_listener(update_listener))
    hass.data[DOMAIN][entry.entry_id] = coordinator

async def async_unload_entry(hass, entry):
    # All listeners, timers, tasks must be cancelled
    hass.data[DOMAIN].pop(entry.entry_id)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
```

### Platform Communication

```python
# ✅ Share data via hass.data, notify via dispatcher
from homeassistant.helpers.dispatcher import async_dispatcher_send

hass.data[DOMAIN] = shared_data
async_dispatcher_send(hass, f"{DOMAIN}_updated")

# ✅ Prefix custom event names with domain
hass.bus.async_fire("hsem_solar_update", {...})  # not "solar_update"
```

### Logging Standards

```python
# ✅ Correct logging patterns
_LOGGER = logging.getLogger(__name__)   # for non-planner code
_LOGGER.debug("Value updated to %s", value)  # %-formatting for logging
_LOGGER.info("Important user event")          # no period at end

# ❌ Wrong
_LOGGER.info(f"Value is {value}")    # f-string in logging
_LOGGER.error("Failed.")             # period at end
```

### Type Hints Enforced

```python
# ✅ Modern Python 3.10+ syntax
def get_price(unit: str | None = None) -> float:
    ...

# ❌ Old syntax
from typing import Optional
def get_price(unit: Optional[str] = None) -> float:
```

## Bronze/Silver Quick Checklist

**Bronze (every integration):**
- [ ] No hardcoded strings — use HA constants
- [ ] Correct entity MRO (CoordinatorEntity, RestoreEntity, SensorEntity)
- [ ] Unique ID and device_info on every entity
- [ ] Config flow returns proper dicts, no state leaks
- [ ] manifest.json dependencies pinned

**Silver (expected quality):**
- [ ] Async patterns correct, no blocking on event loop
- [ ] DataUpdateCoordinator for periodic polling
- [ ] Full translations for all user-facing strings
- [ ] Proper unload: all listeners/timers/tasks cleaned up
- [ ] Platform communication via dispatcher, domain-prefixed events
- [ ] Logging: %-formatting, no periods, proper levels
- [ ] Modern type hints with `| None`
