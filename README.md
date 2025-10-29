# Faikin Estimated Power (Home Assistant + Pyscript)

Derive **instant power (W)** per Faikin device in Home Assistant by:
- listening to Faikin’s **lifetime energy (Wh)**,
- computing **ΔWh / Δt → W** when the counter ticks (Daikin/pydaikin-style),
- **holding** that value for the last tick’s duration + a configurable margin,
- auto-expiring to **0 W** until the next tick.

Works with **multiple Faikins** automatically (wildcard subscribe + MQTT Discovery).  
Optional **compressor fallback** can publish a live estimate between energy ticks.

---

## Requirements

- Home Assistant (Core, OS, or Supervised)
- An MQTT broker and HA’s **MQTT integration** connected
- **HACS** installed (to install Pyscript)
- Faikin devices publishing JSON to `state/<unit>` with at least:
  - `Wh` (lifetime energy, in Wh)
  - `up` (boolean online flag)
  - optionally `comp`, `fanfreq`, `mode` (for fallback)

---

## Install

### 1) Install **Pyscript** via HACS
1. HACS → **Integrations** → search **Pyscript: Python scripting** → **Download/Install**.
2. Settings → **Devices & Services** → **Add Integration** → **Pyscript**.
   - **Allow all imports?** → **Off**
   - **Access `hass` as global?** → **Off**

### 2) Put the script in the right folder
Create the app directory and copy the script from this repo:

```
/config/pyscript/apps/faikin_power/__init__.py
```

(or `/pyscript/apps/faikin_power/__init__.py` if you're using HA "File editor" addon)

> If `pyscript/` or `apps/` doesn’t exist yet, create those folders. `pyscript` directory should exist if you installed and added pyscript integration successfully.

### 3) Configure (optional)
Add (or merge) this in `/config/configuration.yaml`:

```yaml
pyscript:
  apps:
    faikin_power:
      # Topics
      status_prefix: "state"            # Faikin status lives at state/<unit>
      discovery_prefix: "homeassistant" # MQTT Discovery prefix

      # Hold window = last Δt + margin_seconds  (or Δt * (1 + margin_factor))
      margin_seconds: 300               # additive buffer (default 5 min)
      margin_factor: 0.5                # alternative: +50% of last Δt (use one or the other)

      # Publishing options
      min_power_w: 0                    # minimum nonzero floor; 0 disables
      enable_comp_fallback: false       # publish comp-based estimate between ticks
      log_level: "info"                 # "debug" for verbose logs
```

### 4) Reload
- Settings → **Devices & Services** → **Pyscript** → ⋮ → **Reload**
- (Or restart Home Assistant)

---

## What it creates

For each Faikin unit that publishes to `state/<unit>`, the app publishes an MQTT
Discovery config and HA auto-creates:

- `sensor.<unit>_power` (e.g. `sensor.faikin_loznice_power`)
  - `device_class: power`, `unit_of_measurement: W`, `state_class: measurement`
  - Availability from `state/<unit>` (`{"up": true|false}`)

The live state is also published to:
- `faikin/<unit>/power_w` (retained)

---

## How it works (in short)

1. On each **increase** in `Wh`, compute:
   ```
   watts = (ΔWh * 3600) / Δt_seconds
   ```
2. **Publish** that value immediately and **hold** it valid until:
   ```
   hold_until = last_tick_time + (Δt + margin_seconds)
   ```
   or, if configured:
   ```
   hold_until = last_tick_time + Δt * (1 + margin_factor)
   ```
3. If **no new tick** arrives by `hold_until`, publish **0 W**.

> Daikin often increments energy in **100 Wh** steps, so ticks may be **10–15 min** apart at modest loads. Between ticks, power shows **0** unless you enable the **compressor fallback**.

---

## Optional: live value between ticks (compressor fallback)

Enable in `configuration.yaml`:

```yaml
pyscript:
  apps:
    faikin_power:
      enable_comp_fallback: true
      log_level: debug
```

This publishes a simple estimate from `comp`/`fanfreq` **outside** the hold window.
Energy-derived values always override when an energy tick arrives.

---

## Verify

- **Entity appears**: search for **Faikin <unit> Power** in Settings → Devices & Services → Entities.
- **Values**:
  - Non-zero right after an energy tick; back to **0** after the hold window.
  - With fallback enabled, you’ll see updates between ticks too.

Tip: set `log_level: debug` in the app config and check Settings → System → **Logs** for lines starting `[faikin_power]`.

---

## Troubleshooting

### No updates for a while
- Normal at low load: Daikin energy ticks can be far apart.
- If AC is off, sensor drops to **0 W** **after** the hold window (last Δt + margin).
- Reduce `margin_seconds` if you want a faster drop.

---

## Directory layout

```
/config
└─ pyscript
   └─ apps
      └─ faikin_power
         └─ __init__.py
```

---

## Uninstall

1. Remove `/config/pyscript/apps/faikin_power/` (or rename it).
2. **Reload Pyscript.**
3. (Optional) Clear retained discovery configs by publishing empty retained payloads to:
   ```
   homeassistant/sensor/<unit>_power/config
   ```

