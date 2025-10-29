# /config/pyscript/apps/faikin_power/__init__.py
# Faikin Estimated Power (HA-native, multi-device, direct MQTT subscribe)
#
# What this does
#  - Subscribes (via Pyscript) to Faikin state topics:  state/<unit>
#  - Implements Daikin/pydaikin-style “current power consumption” from lifetime energy:
#       est_power (W) = ΔWh / Δt  (held for the last tick’s Δt + a margin, then 0)
#  - Publishes one MQTT Discovery sensor per unit, so HA auto-creates entities
#  - (Optional) Between 100 Wh ticks, can publish a compressor-based estimate
#
# Requirements
#  - Home Assistant’s MQTT integration configured
#  - Pyscript installed & enabled
#
# Tuning (in configuration.yaml):
#   pyscript:
#     apps:
#       faikin_power:
#         status_prefix: "state"
#         discovery_prefix: "homeassistant"
#         margin_seconds: 300          # or use margin_factor: 0.15 (15%)
#         min_power_w: 0               # minimum floor when power > 0; 0 disables
#         enable_comp_fallback: false  # publish comp-based estimates between ticks
#         log_level: "info"            # or "debug"

from datetime import datetime, timezone
import json

# ----------------------- App configuration -----------------------
cfg = pyscript.app_config or {}
STATUS_PREFIX          = cfg.get("status_prefix",         "state")
DISCOVERY_PREFIX       = cfg.get("discovery_prefix",      "homeassistant")
MARGIN_SECONDS_DEFAULT = float(cfg.get("margin_seconds",  300))  # used if margin_factor is not set
MARGIN_FACTOR          = cfg.get("margin_factor",         0.5)  # e.g. 0.5 means +50% of last Δt
MIN_POWER_W            = float(cfg.get("min_power_w",     0))
ENABLE_COMP_FALLBACK   = bool(cfg.get("enable_comp_fallback", False))
LOG_LEVEL              = str(cfg.get("log_level", "info")).lower()

def _log_debug(msg): 
    if LOG_LEVEL == "debug":
        log.debug(msg)
def _log_info(msg): 
    if LOG_LEVEL in ("info", "debug", ""):
        log.info(msg)

# ----------------------- Internal state --------------------------
# Per-unit rolling state
# unit -> dict(last_wh:int, last_ts:float, hold_until:float, last_w:float, discovered:bool, dev_id:str)
_units = {}

# ----------------------- MQTT Discovery --------------------------
def _discovery(unit: str, dev_id: str, config_url: str | None = None):
    """Publish MQTT Discovery for the unit's estimated power sensor."""
    rec = _units.setdefault(unit, {})
    if rec.get("discovered"):
        return

    object_id   = f"{unit}_power"  # simple object_id prevents doubled names
    cfg_topic   = f"{DISCOVERY_PREFIX}/sensor/{object_id}/config"
    state_topic = f"faikin/{unit}/power_w"

    device = {
        "identifiers": [dev_id],
        "manufacturer": "RevK",
        "model": "Faikin",
        "name": f"faikin-{unit}",
    }
    if config_url:
        device["configuration_url"] = config_url

    payload = {
        "name": f"Faikin {unit} Power",
        "unique_id": f"{dev_id}_power",
        "state_topic": state_topic,
        "unit_of_measurement": "W",
        "device_class": "power",
        "state_class": "measurement",
        "icon": "mdi:flash",

        # ✅ Explicit availability strings; template returns "true"/"false"
        "availability_topic": f"{STATUS_PREFIX}/{unit}",
        "availability_template": "{{ (value_json.up | default(true)) | string | lower }}",
        "payload_available": "true",
        "payload_not_available": "false",

        "device": device,
    }
    mqtt.publish(topic=cfg_topic, payload=json.dumps(payload), qos=1, retain=True)
    rec["discovered"] = True
    _units[unit] = rec
    _log_info(f"[faikin_power] discovery published for unit={unit} device_id={dev_id}")
    _log_info(f"[faikin_power] discovery topic: {cfg_topic}")

def _publish_power(unit: str, watts: float):
    """Publish the power to the per-unit MQTT state topic and remember last value."""
    if watts < 0:
        watts = 0.0
    if MIN_POWER_W and watts > 0:
        watts = max(watts, MIN_POWER_W)
    mqtt.publish(topic=f"faikin/{unit}/power_w", payload=str(round(float(watts), 1)), qos=0, retain=True)
    _units.setdefault(unit, {})["last_w"] = float(watts)
    _log_debug(f"[faikin_power] publish {unit}: {watts:.1f} W")

def _compute_hold_seconds(last_dt_seconds: float) -> float:
    """Hold window per pydaikin behavior: last Δt plus either a timedelta-like margin, or a factor."""
    if MARGIN_FACTOR is not None:
        try:
            fac = float(MARGIN_FACTOR)
            return max(0.0, last_dt_seconds * (1.0 + fac))
        except Exception:
            pass
    # fallback: additive seconds
    return max(0.0, last_dt_seconds + MARGIN_SECONDS_DEFAULT)

# ----------------------- Energy-derived power (pydaikin-style) ---
def _update_from_energy(unit: str, wh_now: int, ts_now: float):
    """
    Mirror Daikin/pydaikin 'current_power_consumption' behavior in HA terms:
      - If lifetime energy (Wh) increased by ΔWh over Δt seconds,
        est_power = ΔWh * 3600 / Δt  (W)
      - Keep that value valid until hold_until = ts_now + Δt + margin
      - After hold_until, publish 0 W
    """
    rec = _units.setdefault(unit, {})
    last_wh = rec.get("last_wh")
    last_ts = rec.get("last_ts")

    # First observation — initialise & publish 0 (so entity shows up cleanly)
    if last_wh is None or last_ts is None:
        rec["last_wh"] = int(wh_now)
        rec["last_ts"] = float(ts_now)
        rec["hold_until"] = 0.0
        _units[unit] = rec
        _publish_power(unit, 0.0)
        return

    # Counter reset/rollback
    if wh_now < last_wh:
        rec["last_wh"] = int(wh_now)
        rec["last_ts"] = float(ts_now)
        rec["hold_until"] = 0.0
        _units[unit] = rec
        _publish_power(unit, 0.0)
        return

    # No change, but we may need to expire to 0 if the hold window passed
    if wh_now == last_wh:
        hold_until = rec.get("hold_until", 0.0)
        if hold_until and ts_now > hold_until:
            _publish_power(unit, 0.0)
            rec["hold_until"] = 0.0
            _units[unit] = rec
        return

    # We have a tick: compute power from ΔWh / Δt
    dwh = float(wh_now - last_wh)
    dt  = max(0.5, float(ts_now - last_ts))  # guard tiny intervals
    watts = (dwh * 3600.0) / dt

    # Update last_* and hold window per last Δt + margin
    rec["last_wh"] = int(wh_now)
    rec["last_ts"] = float(ts_now)
    hold_seconds   = _compute_hold_seconds(dt)
    rec["hold_until"] = ts_now + hold_seconds
    _units[unit] = rec

    # Publish estimated power now; it will expire to 0 after hold window
    _publish_power(unit, watts)

# ----------------------- Compressor fallback (optional) ----------
def estimate_power_from_comp(comp_hz: float, fanfreq: float | None = None) -> float:
    """
    Optional stopgap to provide a 'live' estimate between 100 Wh ticks.
    Replace this with your own mapping if you want better fidelity.
    """
    if comp_hz is None or comp_hz <= 0:
        return 0.0
    # Simple placeholder: ~50 W per 'comp' unit. Tune if you enable this path.
    return max(0.0, float(comp_hz) * 50.0)

# ----------------------- MQTT subscriptions (direct) -------------
# Energy tick handler — only runs when payload contains 'Wh'
@mqtt_trigger(f"{STATUS_PREFIX}/+", "payload_obj and ('Wh' in payload_obj)")
def faikin_energy_tick(topic=None, payload_obj=None, **kwargs):
    # topic: state/<unit>
    try:
        unit = topic.split("/", 1)[1]
    except Exception:
        return

    dev_id = str(payload_obj.get("id") or f"faikin-{unit}")
    _discovery(unit, dev_id, config_url=f"http://{unit}.local/")

    try:
        wh = int(payload_obj["Wh"])
    except Exception:
        return

    ts_now = datetime.now(timezone.utc).timestamp()
    _update_from_energy(unit, wh, ts_now)

# Compressor/fan handler — publishes between ticks unless within hold window.
# Subscription is always active, but we guard on ENABLE_COMP_FALLBACK at runtime.
@mqtt_trigger(f"{STATUS_PREFIX}/+", "payload_obj and (('comp' in payload_obj) or ('fanfreq' in payload_obj))")
def faikin_comp_estimate(topic=None, payload_obj=None, **kwargs):
    if not ENABLE_COMP_FALLBACK:
        return

    try:
        unit = topic.split("/", 1)[1]
    except Exception:
        return

    dev_id = str(payload_obj.get("id") or f"faikin-{unit}")
    _discovery(unit, dev_id, config_url=f"http://{unit}.local/")

    # Skip if we're still within the last energy-tick hold window
    hold_until = _units.get(unit, {}).get("hold_until", 0.0)
    now_ts = datetime.now(timezone.utc).timestamp()
    if hold_until and now_ts <= hold_until:
        return

    comp = payload_obj.get("comp")
    fan  = payload_obj.get("fanfreq")
    try:
        comp = float(comp) if comp is not None else 0.0
        fan  = float(fan) if fan is not None else None
    except Exception:
        comp = 0.0
        fan  = None

    est = estimate_power_from_comp(comp, fan)
    _publish_power(unit, est)
