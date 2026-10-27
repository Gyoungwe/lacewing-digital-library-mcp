# Bundled basemap data

`ne_50m_land.geojson.gz` and `ne_50m_admin0.geojson.gz` are derived from
[Natural Earth](https://www.naturalearthdata.com/) 1:50m Physical "Land" and
Cultural "Admin 0 – Countries" (nvkelso/natural-earth-vector 5.x). Natural Earth
data are in the public domain. Coordinates were rounded to 3 decimal places and
non-geometry attributes removed except `ADMIN`, `NAME`, `NAME_LONG`, `FORMAL_EN`,
`NAME_ZH`, `SOVEREIGNT`, `ISO_A3`, `ISO_A2_EH` and `ADM0_A3` (used for country-name
normalization and point-in-country checks).

Admin-0 boundaries follow Natural Earth's de facto depiction. Check your target
journal's policy on disputed boundaries before publication, or pass
`basemap="<your.geojson>"` to use an approved land layer.
