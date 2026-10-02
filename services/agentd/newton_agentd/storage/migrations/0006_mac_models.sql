-- SV4: models on this Mac (MLX) are off until the user turns them on in the profile.
ALTER TABLE profile ADD COLUMN mac_models INTEGER NOT NULL DEFAULT 0;
