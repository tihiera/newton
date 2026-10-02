-- Per-host GPU support: "auto" installs and verifies CuPy whenever the host has an
-- NVIDIA driver (each bootstrap re-checks it), "off" never touches it.
ALTER TABLE hosts ADD COLUMN gpu_support TEXT NOT NULL DEFAULT 'auto'
    CHECK (gpu_support IN ('auto', 'off'));
