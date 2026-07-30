---
name: Volumetric point cloud algorithm
description: How the image-to-3D pipeline works after removing mesh/Meshy — geometric hemisphere projection producing a lenticular particle volume.
---

The 3D viewport receives `{type: "points", points: [{x,y,z,color},...]}` from `_image_to_volumetric_points` in `backend/main.py`.

**Algorithm (Geometric Hemisphere Projection):**
- Grid: 80×50 pixels resampled from Pollinations image
- Foreground mask: BFS flood-fill from corners + morphological opening (`_open_mask`)
- Centroid: mean (col, row) of all foreground pixels
- Per foreground pixel at radial distance `dr` from centroid:
  - `rn = dr / max_silhouette_dist`
  - `geo_z = sqrt(max(0, 1 - rn²))` — hemisphere (1 at centre, 0 at edge)
  - `color_z = 1 - luma` — darker pixels slightly more prominent
  - `z_front = Z_MAX * geo_z * (0.8 + 0.2 * color_z)` (Z_MAX = 2.0)
  - `z_back = -Z_MAX * 0.65 * geo_z`
- Each pixel → 2 points: front (true colour) + back (colour − 64 per channel)
- Points are centroid-centred in world space (wx − cx_w, wy − cy_w)

**Why:** Local, zero external deps, no mesh topology. Produces a lenticular solid — thick at visual centre of mass, tapering at silhouette — that looks volumetric from any camera angle.

**How to apply:** If the shape looks flat, increase Z_MAX. If the rear hemisphere is too prominent, lower the 0.65 factor. If the blend looks noisy, reduce the color_z weight (0.2 → 0.1).
