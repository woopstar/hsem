"""OCPP client certificate service (issue #1341).

``ocpp_generate_client_certificate`` signs a client key and certificate
for one charge point with the CA HSEM generates for its OCPP server, for
chargers that need a certificate uploaded before they can use mutual TLS.

Split from :mod:`services_ocpp`, whose services all talk to a connected
charger; this one only writes files and works with no charger connected.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.core import ServiceCall, ServiceResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.misc import get_config_value
from custom_components.hsem.utils.ocpp_tls import (
    CPID_PATTERN,
    OCPPTLSError,
    issue_client_certificate,
    resolve_tls_dir,
)
from custom_components.hsem.utils.schema import vol

SERVICE_OCPP_GENERATE_CLIENT_CERTIFICATE = "ocpp_generate_client_certificate"

SCHEMA_OCPP_GENERATE_CLIENT_CERTIFICATE = vol.Schema(
    {
        vol.Required("cpid"): vol.All(vol.Coerce(str), vol.Match(CPID_PATTERN)),
        vol.Optional("replace", default=False): vol.Coerce(bool),
    }
)


async def async_handle_ocpp_generate_client_certificate(
    call: ServiceCall,
) -> ServiceResponse:
    """Sign a client key and certificate (``CN=<cpid>``) with HSEM's CA.

    Generates the CA first if it does not exist yet. The files land in
    ``<tls folder>/clients/`` and only their paths are returned: key
    material never leaves the filesystem through this service.

    Args:
        call: The service call with ``cpid`` (the charge point identity the
            charger connects with) and optional ``replace`` (overwrite an
            existing certificate for that identity, default ``False``).

    Returns:
        ``certificate_path``, ``key_path`` and ``ca_certificate_path``.

    Raises:
        ServiceValidationError: When a custom client CA is configured
            (HSEM cannot sign with a CA whose key it does not hold), or a
            certificate for this identity exists and ``replace`` is off.
        HomeAssistantError: When the files cannot be written.
    """
    hass = call.hass
    cpid: str = call.data["cpid"]
    replace: bool = call.data["replace"]

    entries = hass.config_entries.async_entries(DOMAIN)
    if entries and get_config_value(entries[0], "hsem_ocpp_tls_client_ca_path"):
        raise ServiceValidationError(
            "HSEM service: a custom client CA is configured, so certificates "
            "signed by HSEM's CA would be refused. Clear the client CA path to "
            "use HSEM's CA, or sign the certificate with your own CA."
        )

    config_dir = Path(hass.config.config_dir)
    now = dt_util.utcnow()
    try:
        # Resolving the folder checks the filesystem, so it runs in the
        # executor along with key generation.
        paths = await hass.async_add_executor_job(
            lambda: issue_client_certificate(
                resolve_tls_dir(config_dir), cpid, now, replace=replace
            )
        )
    except FileExistsError as err:
        raise ServiceValidationError(
            f"HSEM service: a client certificate for '{cpid}' already exists "
            f"({err}). Call again with replace: true to issue a new one."
        ) from err
    except (OCPPTLSError, OSError) as err:
        raise HomeAssistantError(
            f"HSEM service: cannot issue a client certificate for '{cpid}': {err}"
        ) from err

    _LOGGER.info(
        "HSEM service: issued OCPP client certificate %s for cpid=%s",
        paths.certificate,
        cpid,
    )
    return {
        "certificate_path": str(paths.certificate),
        "key_path": str(paths.key),
        "ca_certificate_path": str(paths.ca_certificate),
    }
