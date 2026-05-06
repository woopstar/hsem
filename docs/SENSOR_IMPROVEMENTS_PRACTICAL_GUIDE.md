# Sensor Implementation Improvements: Practical Guide

## Problem Statement

You're experiencing data loss because:

1. **No Persistent Storage** - avg_sensor only saves to HA state attributes (volatile)
2. **Timer-Only Updates** - 5-minute intervals miss rapid changes
3. **No Validation** - Corrupted data is restored silently
4. **Dependency Chain** - Single failure breaks all downstream sensors
5. **Manual Reset Required** - You have to delete/recreate sensors regularly

## Solution: Enhanced Sensors with Persistence

### Part A: Enhanced avg_sensor (with Persistent Storage)

```python
# File: custom_components/hsem/custom_sensors/avg_sensor_enhanced.py

import json
import logging
import os
import hashlib
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorStateClass
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.misc import ha_get_entity_state_and_convert

_LOGGER = logging.getLogger(__name__)

# Data directory for persistent storage
DATA_DIR = "custom_components/hsem/data"


class HSEMAvgSensorEnhanced(SensorEntity, HSEMEntity, RestoreEntity):
    """Enhanced avg sensor with persistent storage and validation."""

    _attr_icon = "mdi:calculator"
    _attr_has_entity_name = True

    _unrecorded_attributes = frozenset(
        ["tracked_entity", "average", "hour_start", "hour_end", "unique_id"]
    )

    def __init__(
        self,
        config_entry,
        hour_start,
        hour_end,
        avg,
        tracked_entity,
        name,
        unique_id,
        entity_id,
    ) -> None:
        super().__init__(config_entry)
        self._hour_start = hour_start
        self._hour_end = hour_end
        self._average = avg
        self._tracked_entity = tracked_entity
        self._attr_unique_id = unique_id
        self.entity_id = entity_id
        self._state = None
        self._last_updated = None
        self._config_entry = config_entry
        self._name = name
        self._measurements = {}  # date ISO → consumption
        self._tracked_entities = set()
        
        # Persistent storage
        self._data_file = self._get_data_file_path()
        self._last_known_good = {}  # Fallback if restore fails
        self._validation_status = "pending"  # pending, valid, corrupted
        self._validation_errors = []

    def _get_data_file_path(self) -> str:
        """Get the path for persistent storage file."""
        data_dir = Path(self.hass.config.path(DATA_DIR)) if self.hass else Path(DATA_DIR)
        data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir / f"{self._attr_unique_id}_measurements.json")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return state attributes including validation info."""
        return {
            "tracked_entity": self._tracked_entity,
            "average": self._average,
            "hour_start": self._hour_start,
            "hour_end": self._hour_end,
            "last_updated": self._last_updated,
            "unique_id": self._attr_unique_id,
            "measurements_count": len(self._measurements),
            "validation_status": self._validation_status,
            "validation_errors": self._validation_errors,
            # Include compressed version for quick restore
            "measurements": self._measurements,
        }

    @property
    def state(self) -> float | None:
        return self._state

    @property
    def unit_of_measurement(self) -> str:
        return UnitOfEnergy.KILO_WATT_HOUR

    @property
    def state_class(self) -> str:
        return SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def should_poll(self) -> bool:
        return True

    async def async_update(self, event=None) -> None:
        """Manually trigger the sensor update."""
        return await self._async_handle_update(event)

    def _parse_date(self, date_str: str) -> str:
        """Parse and normalize date string to ISO format."""
        try:
            date_part = date_str.split("T")[0] if "T" in date_str else date_str
            return datetime.strptime(date_part, "%Y-%m-%d").date().isoformat()
        except (ValueError, TypeError):
            _LOGGER.warning(f"Invalid date format: {date_str}")
            return None

    def _calculate_hash(self, data: dict) -> str:
        """Calculate SHA256 hash of measurements for integrity check."""
        json_str = json.dumps(data, sort_keys=True)
        return hashlib.sha256(json_str.encode()).hexdigest()

    async def _async_load_persistent_measurements(self) -> bool:
        """Load measurements from persistent JSON file."""
        if not os.path.exists(self._data_file):
            return False

        try:
            with open(self._data_file, 'r') as f:
                data = json.load(f)

            # Validate structure
            if not isinstance(data, dict) or "measurements" not in data:
                _LOGGER.error(f"Invalid persistent data format in {self._data_file}")
                self._validation_errors.append("Invalid format")
                self._validation_status = "corrupted"
                return False

            # Verify hash
            stored_hash = data.get("hash")
            measurements = data.get("measurements", {})
            calculated_hash = self._calculate_hash(measurements)

            if stored_hash and stored_hash != calculated_hash:
                _LOGGER.error(f"Hash mismatch in persistent storage for {self.entity_id}")
                self._validation_errors.append("Hash mismatch - possible corruption")
                self._validation_status = "corrupted"
                return False

            # Validate dates
            valid_measurements = {}
            for date_str, value in measurements.items():
                normalized_date = self._parse_date(date_str)
                if normalized_date is None:
                    self._validation_errors.append(f"Invalid date: {date_str}")
                    continue
                try:
                    valid_measurements[normalized_date] = float(value)
                except (ValueError, TypeError):
                    self._validation_errors.append(f"Invalid value for {date_str}")
                    continue

            self._measurements = valid_measurements
            self._last_known_good = valid_measurements.copy()
            self._validation_status = "valid"
            _LOGGER.info(
                f"Loaded {len(self._measurements)} measurements from persistent storage for {self.entity_id}"
            )
            return True

        except Exception as e:
            _LOGGER.error(f"Failed to load persistent measurements: {e}")
            self._validation_errors.append(f"Load error: {str(e)}")
            self._validation_status = "corrupted"
            return False

    async def _async_persist_measurements(self) -> bool:
        """Save measurements to persistent JSON file."""
        try:
            os.makedirs(os.path.dirname(self._data_file), exist_ok=True)

            # Prepare data with metadata
            data = {
                "version": 1,
                "entity_id": self.entity_id,
                "last_updated": datetime.now().isoformat(),
                "count": len(self._measurements),
                "measurements": self._measurements,
                "hash": self._calculate_hash(self._measurements),
            }

            # Write to temporary file first
            temp_file = self._data_file + ".tmp"
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=2)

            # Atomic rename
            if os.path.exists(self._data_file):
                os.remove(self._data_file)
            os.rename(temp_file, self._data_file)

            return True
        except Exception as e:
            _LOGGER.error(f"Failed to persist measurements: {e}")
            return False

    async def async_added_to_hass(self) -> None:
        """Handle when sensor is added to Home Assistant."""
        
        # Try to restore from persistent storage first (most reliable)
        persistent_loaded = await self._async_load_persistent_measurements()

        # Fallback: restore from HA state if persistent failed
        if not persistent_loaded:
            old_state = await self.async_get_last_state()
            if old_state is not None:
                try:
                    self._state = float(old_state.state)
                    restored_measurements = old_state.attributes.get("measurements", {})
                    if isinstance(restored_measurements, dict):
                        self._measurements = {
                            self._parse_date(k): round(float(v), 2)
                            for k, v in restored_measurements.items()
                            if self._parse_date(k) is not None
                        }
                        self._validation_status = "valid"
                    self._last_updated = old_state.attributes.get("last_updated")
                except Exception as e:
                    _LOGGER.warning(f"Failed to restore from HA state: {e}")

        # Add event listener (immediate updates on source change)
        async_track_state_change_event(
            self.hass,
            [self._tracked_entity],
            self._async_on_state_change,
        )

        # Add timer for periodic validation (5-minute safety net)
        async_track_time_interval(
            self.hass, self._async_on_timer, timedelta(minutes=5)
        )

        # Initial update
        await self._async_handle_update(None)

        await super().async_added_to_hass()

    async def _async_on_state_change(self, event) -> None:
        """React immediately to source entity state changes."""
        await self._async_handle_update(event)

    async def _async_on_timer(self, now) -> None:
        """Periodic validation and maintenance."""
        # Validate measurements integrity
        await self._async_validate_measurements()
        # Persist to disk
        await self._async_persist_measurements()
        # Cleanup old measurements
        await self._async_cleanup_old_measurements()

    async def _async_validate_measurements(self) -> None:
        """Validate that measurements are consistent and correct."""
        errors = []

        if not self._measurements:
            errors.append("No measurements stored")
        else:
            # Check for negative values
            negative = [d for d, v in self._measurements.items() if v < 0]
            if negative:
                errors.append(f"Negative values: {negative}")

            # Check for extreme values (> 300 kWh in an hour is unrealistic)
            extreme = [d for d, v in self._measurements.items() if v > 300]
            if extreme:
                errors.append(f"Extreme values: {extreme}")

            # Check for gaps
            dates = sorted(self._measurements.keys())
            if dates:
                start_date = datetime.fromisoformat(dates[0]).date()
                end_date = datetime.fromisoformat(dates[-1]).date()
                expected_days = (end_date - start_date).days + 1
                actual_days = len(dates)
                if actual_days < expected_days * 0.8:  # Allow 20% gap
                    errors.append(
                        f"Too many gaps: {actual_days} days of {expected_days} expected"
                    )

        if errors:
            _LOGGER.warning(f"Measurement validation issues: {errors}")
            self._validation_errors = errors
            self._validation_status = "valid_with_warnings"
        else:
            self._validation_errors = []
            self._validation_status = "valid"

    async def _async_track_entities(self) -> None:
        """Track state changes for source entity."""
        if self._tracked_entity:
            if self._tracked_entity not in self._tracked_entities:
                self._tracked_entities.add(self._tracked_entity)

    async def _async_handle_update(self, event=None) -> None:
        """Handle sensor update."""
        self._state = 0.0

        now = datetime.now()
        await self._async_track_entities()
        await self._async_store_utility_meter_value()

        # Calculate average from measurements
        if self._measurements:
            total = sum(self._measurements.values())
            count = len(self._measurements)
            if count > 0:
                self._state = round(total / count, 2)

        self._last_updated = now.isoformat()
        self.async_write_ha_state()

    async def _async_store_utility_meter_value(self) -> None:
        """Store current meter value for the day."""
        now = datetime.now()
        current_date = now.date().isoformat()

        try:
            utility_meter_value = ha_get_entity_state_and_convert(
                self, self._tracked_entity, "float"
            )
        except Exception as e:
            _LOGGER.warning(f"Failed to get utility meter value: {e}")
            return

        if utility_meter_value is not None:
            try:
                value = round(float(utility_meter_value), 2)
                self._measurements[current_date] = value
                # Persist immediately on change
                await self._async_persist_measurements()
            except (ValueError, TypeError) as e:
                _LOGGER.warning(f"Invalid utility meter value: {e}")

    async def _async_cleanup_old_measurements(self) -> None:
        """Remove measurements older than the configured average window."""
        if not self._measurements or len(self._measurements) <= self._average:
            return

        sorted_dates = sorted(self._measurements.keys())
        items_to_remove = len(sorted_dates) - self._average

        for date_key in sorted_dates[:items_to_remove]:
            del self._measurements[date_key]

        await self._async_persist_measurements()
```

### Part B: Integration Sensor with Backup Restoration

```python
# File: custom_components/hsem/custom_sensors/integration_sensor_enhanced.py

import json
import logging
import os
from pathlib import Path

from homeassistant.components.integration.sensor import IntegrationSensor
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.entity import HSEMEntity

_LOGGER = logging.getLogger(__name__)
DATA_DIR = "custom_components/hsem/data"


class HSEMIntegrationSensorEnhanced(IntegrationSensor, HSEMEntity, RestoreEntity):
    """Integration sensor with backup state restoration."""

    _attr_icon = "mdi:chart-histogram"

    def __init__(self, *args, id: str, e_id: str, config_entry=None, **kwargs) -> None:
        IntegrationSensor.__init__(self, *args, **kwargs)
        HSEMEntity.__init__(self, config_entry)
        self._attr_unique_id = id
        self.entity_id = e_id
        self._backup_file = Path(self.hass.config.path(DATA_DIR)) / f"{id}_backup.json" if hasattr(self, 'hass') else None

    @property
    def state_class(self) -> str:
        return SensorStateClass.TOTAL

    @property
    def device_class(self) -> str:
        return SensorDeviceClass.ENERGY

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    def should_poll(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        """Initialize and restore state."""
        # First try to restore from HA's RestoreEntity
        old_state = await self.async_get_last_state()
        if old_state is not None:
            try:
                self._state = float(old_state.state)
                _LOGGER.debug(f"Restored {self.entity_id} from HA state: {self._state}")
            except (ValueError, TypeError):
                # Try backup file
                await self._async_restore_from_backup()
        else:
            # No HA state, try backup
            await self._async_restore_from_backup()

        await super().async_added_to_hass()

    async def _async_restore_from_backup(self) -> None:
        """Restore from backup file if available."""
        if not self._backup_file or not self._backup_file.exists():
            _LOGGER.warning(f"No backup found for {self.entity_id}")
            return

        try:
            with open(self._backup_file, 'r') as f:
                backup_data = json.load(f)
            self._state = float(backup_data.get("state", 0.0))
            _LOGGER.info(f"Restored {self.entity_id} from backup: {self._state}")
        except Exception as e:
            _LOGGER.error(f"Failed to restore from backup: {e}")

    async def async_update(self, *args, **kwargs) -> None:
        """Update and backup state."""
        await super().async_update(*args, **kwargs)
        
        # Save backup
        if self._state is not None:
            try:
                os.makedirs(self._backup_file.parent, exist_ok=True)
                with open(self._backup_file, 'w') as f:
                    json.dump({"state": self._state, "entity_id": self.entity_id}, f)
            except Exception as e:
                _LOGGER.warning(f"Failed to backup state: {e}")
```

### Part C: Utility Meter Sensor with Independent Tracking

```python
# File: custom_components/hsem/custom_sensors/utility_meter_sensor_enhanced.py

import json
import logging
import os
from pathlib import Path

from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.components.utility_meter.sensor import UtilityMeterSensor
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.misc import ha_get_entity_state_and_convert

_LOGGER = logging.getLogger(__name__)
DATA_DIR = "custom_components/hsem/data"


class HSEMUtilityMeterSensorEnhanced(UtilityMeterSensor, HSEMEntity, RestoreEntity):
    """Utility meter sensor with independent cumulative tracking."""

    _attr_icon = "mdi:counter"

    def __init__(self, *args, id: str, e_id: str, config_entry=None, **kwargs) -> None:
        UtilityMeterSensor.__init__(self, *args, **kwargs)
        HSEMEntity.__init__(self, config_entry)
        self._attr_unique_id = id
        self.entity_id = e_id
        
        # Independent tracking (backup if parent sensor fails)
        self._cumulative_total = 0.0
        self._last_source_value = 0.0
        self._backup_file = Path(self.hass.config.path(DATA_DIR)) / f"{id}_cumulative.json" if hasattr(self, 'hass') else None

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    def unit_of_measurement(self) -> str:
        return UnitOfEnergy.KILO_WATT_HOUR

    @property
    def device_class(self) -> str:
        return SensorDeviceClass.ENERGY

    @property
    def state_class(self) -> str:
        return SensorStateClass.TOTAL

    @property
    def should_poll(self) -> bool:
        return True

    async def async_added_to_hass(self) -> None:
        """Initialize and restore cumulative tracking."""
        # Restore independent cumulative total
        await self._async_restore_cumulative()
        await super().async_added_to_hass()

    async def _async_restore_cumulative(self) -> None:
        """Restore cumulative total from backup."""
        if not self._backup_file or not self._backup_file.exists():
            return

        try:
            with open(self._backup_file, 'r') as f:
                backup_data = json.load(f)
            self._cumulative_total = float(backup_data.get("cumulative_total", 0.0))
            self._last_source_value = float(backup_data.get("last_source_value", 0.0))
            _LOGGER.info(
                f"Restored cumulative total for {self.entity_id}: {self._cumulative_total} kWh"
            )
        except Exception as e:
            _LOGGER.warning(f"Failed to restore cumulative total: {e}")

    async def _async_persist_cumulative(self) -> None:
        """Persist cumulative total to backup file."""
        if not self._backup_file:
            return

        try:
            os.makedirs(self._backup_file.parent, exist_ok=True)
            with open(self._backup_file, 'w') as f:
                json.dump({
                    "cumulative_total": self._cumulative_total,
                    "last_source_value": self._last_source_value,
                    "entity_id": self.entity_id,
                }, f)
        except Exception as e:
            _LOGGER.warning(f"Failed to persist cumulative total: {e}")

    async def async_update(self, *args, **kwargs) -> None:
        """Update and track cumulative independently."""
        # Get current source value
        try:
            current_source = ha_get_entity_state_and_convert(
                self, self.source_entity_id, "float"
            )
        except Exception:
            current_source = None

        # Update independent cumulative total
        if current_source is not None:
            delta = current_source - self._last_source_value
            if delta >= 0:  # Only count increases
                self._cumulative_total += delta
                self._last_source_value = current_source
            # Fallback to our total if parent sensor fails
            self._state = self._cumulative_total

        await super().async_update(*args, **kwargs)
        
        # Persist after update
        await self._async_persist_cumulative()
```

---

## Implementation Steps

1. **Create new enhanced sensor files** alongside existing ones (backward compatible)
2. **Add to manifest.json** platform definitions pointing to new classes
3. **Migrate config** to use enhanced versions: `integration_sensor_enhanced` instead of `integration_sensor`
4. **Test for 1 week** with both running in parallel
5. **Retire old sensors** once enhanced versions proven stable

---

## Benefits

✅ **No more data loss** - Persistent JSON backup  
✅ **Catches rapid changes** - Event-driven updates  
✅ **Self-healing** - Validation and fallbacks  
✅ **No manual resets** - Automatic restoration  
✅ **Transparency** - Validation status exposed as attributes
