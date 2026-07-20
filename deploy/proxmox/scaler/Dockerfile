FROM python:3.12-slim AS base

WORKDIR /app

# Install runtime deps first so the dependency layer caches across code changes.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the source.
COPY esphome_fleet_proxmox_scaler /app/esphome_fleet_proxmox_scaler

# Run as non-root.
RUN useradd --system --uid 1000 --no-create-home scaler
USER scaler

ENTRYPOINT ["python", "-m", "esphome_fleet_proxmox_scaler"]
