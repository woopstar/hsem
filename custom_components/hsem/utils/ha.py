"""
Set the selected option for an entity.

This method makes an asynchronous service call to set the specified option
for the given entity_id. If the entity_id does not exist, it logs an error
and exits the method.

Args:
    entity_id (str): The ID of the entity for which the option is to be set.
    option (str): The option to be set for the entity.

Raises:
    Exception: If the service call fails, an exception is raised and an error
    is logged with the failure details.
"""

import logging
_LOGGER = logging.getLogger(__name__)

async def async_set_select_option(self, entity_id, option):
    """Set the selected option for an entity."""

    # Check if entity_id exists
    entity = self.hass.states.get(entity_id)

    if entity is None:
        _LOGGER.error(f"Entity with id {entity_id} not found.")
        return  # Exit the method if entity_id does not exist

    try:
        # Make the service call to set the option
        await self.hass.services.async_call(
            "select",
            "select_option",
            {
                "entity_id": entity_id,
                "option": option,
            },
            blocking=True,
        )
        _LOGGER.warning(f"Set option '{option}' for entity_id '{entity_id}'")
    except Exception as err:
        _LOGGER.error(
            f"Failed to set option '{option}' for entity_id '{entity_id}': {err}"
        )
        raise
