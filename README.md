# Fuel Route Optimizer

A Django REST API that takes a start and a finish anywhere in the USA. It returns:

- the driving route, as GeoJSON plus a link to an interactive map;
- where to fuel up (500-mile range, 10 mpg);
- how much to buy at each stop;
- what the fuel for the whole trip costs.

Prices come from the provided OPIS truck-stop price list. Routing uses the free
[OSRM](https://project-osrm.org) API. **A request makes one routing call, or none when the
route is cached.**

```
GET /api/v1/route/?start=Chicago, IL&finish=Dallas, TX
```

```jsonc
{
  "start":  {"query": "Chicago, IL", "label": "Chicago, IL", "latitude": 41.837, "longitude": -87.685, "source": "gazetteer"},
  "finish": {"query": "Dallas, TX",  "label": "Dallas, TX",  "latitude": 32.793, "longitude": -96.767, "source": "gazetteer"},
  "route":  {"distance_miles": 961.5, "duration_hours": 17.04, "provider": "osrm"},
  "vehicle": {"max_range_miles": 500, "mpg": 10, "tank_capacity_gallons": 50},
  "fuel_stops": [
    {"stop": 1, "name": "QUIKTRIP #7204", "address": "I-57, EXIT 337 & SR-50", "city": "University Park", "state": "IL",
     "route_mile": 29.5, "price_per_gallon": 3.079, "fuel_on_arrival_gallons": 2.05, "gallons": 47.099, "cost": 145.02, ...},
    {"stop": 2, "name": "QUIKTRIP #7151", "city": "West Memphis", "state": "AR",
     "route_mile": 521.0, "price_per_gallon": 2.999, "fuel_on_arrival_gallons": 0.0, "gallons": 49.049, "cost": 147.10, ...}
  ],
  "summary": {"total_fuel_cost": 292.12, "total_gallons_purchased": 96.147, "fuel_burned_gallons": 96.147,
              "number_of_stops": 2, "average_price_per_gallon": 3.038, "stations_on_route": 163, ...},
  "map": {"url": "http://127.0.0.1:8000/api/v1/route/map/?start=Chicago%2C+IL&finish=Dallas%2C+TX",
          "geojson": {"type": "FeatureCollection", "features": ["route line", "start", "finish", "fuel stops"]}},
  "assumptions": ["..."],
  "meta": {"cache": {"plan": "miss", "route": "miss"}, "external_api_calls": {"routing": 1, "geocoding": 0},
           "timings_ms": {"route": 1361.7, "match_stations": 8.7, "optimize": 2.2, "total": 1389.8}}
}
```

Open `map.url` in a browser for the interactive map. It shows:

- the route;
- the numbered fuel stops;
- every station along the route, coloured by price.

## Quick start

Requires Python 3.12+.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python manage.py migrate
python manage.py load_stations      # loads the 6,738 stations from data/ (~1 s)
python manage.py runserver
```

`DEBUG` is off unless `DJANGO_DEBUG=1` is set; turning it on also enables DRF's browsable
API. Then try <http://127.0.0.1:8000/> (map with a form) or the API:

```bash
curl "http://127.0.0.1:8000/api/v1/route/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA"
curl -X POST http://127.0.0.1:8000/api/v1/route/ -H "Content-Type: application/json" \
     -d '{"start": {"lat": 41.8781, "lng": -87.6298}, "finish": "1600 Pennsylvania Ave NW, Washington, DC"}'
```

`postman/FuelRouteOptimizer.postman_collection.json` holds ready-made requests with tests. Each
successful request draws the route and stops in Postman's **Visualize** tab.

## API

| Endpoint | Description |
|---|---|
| `GET /api/v1/route/?start=…&finish=…[&stop_cost=…]` | Plan a trip (query string) |
| `POST /api/v1/route/` with JSON `{"start": …, "finish": …, "stop_cost": …}` | Plan a trip (JSON body) |
| `GET /api/v1/route/map/?start=…&finish=…` | The same plan as an interactive Leaflet map |
| `GET /api/v1/health/` | Whether station data is loaded |

**Locations** can be given in three forms, cheapest first:

- `"lat,lng"` text or `{"lat": .., "lng": ..}`: used as is, no lookup.
- `"City, ST"` or `"City, State"`: resolved offline from the US Census gazetteer, no API call.
- Anything else (a street address, landmark or ZIP): one call to Nominatim (OpenStreetMap), cached.

Both points must be inside the USA.

**`stop_cost`** (optional, default `15`) is the dollar cost charged per fuel stop when choosing
stops (see below). Use `0` for the absolute cheapest fuel bill.

**Errors** always have the shape `{"error": {"code", "message", "details"}}`:

| Status | Codes |
|---|---|
| 400 | `invalid_request`, `invalid_location`, `location_not_found`, `outside_usa`, `same_location` |
| 422 | `route_not_found` (e.g. Hawaii), `no_feasible_fuel_plan` (a stretch longer than 500 miles has no station; `details` says where) |
| 503 | `upstream_unavailable` (routing/geocoding outage), `station_data_missing` (run `load_stations`) |

## How it works

```
start, finish ──► geocode (offline gazetteer; Nominatim only for free text)
              ──► route   (1 OSRM call, cached 24 h)
              ──► stations within 5 miles of the route   (KD-tree, ~10 ms)
              ──► optimise where/how much to buy          (DP + greedy, ~3 ms)
              ──► JSON + GeoJSON (plan cached 1 h)
```

### 1. Station coordinates (offline, done once)

The price list has no coordinates, only a city and state for each truck stop.
`manage.py build_geodata` resolves all 3,898 cities to coordinates with no geocoding API. It
matches the names against the US Census gazetteer first, then GeoNames. The names are
normalised so that "Mc Calla" matches "McCalla" and "Saint Johns" matches "St. Johns".

Some states have several places with the same name; Tennessee has six Antiochs. For those,
the build picks the candidate nearest the other stations in the same OPIS rack (wholesale
market). Five oddities are listed in `data/geocode_overrides.csv`. The results are committed in
`data/`, so the API never needs this step.

### 2. Stations on the route

OSRM returns the full route geometry, which the API decodes with numpy (~35k points in a few
milliseconds). The route is then resampled every 0.5 miles and converted to 3D unit vectors in
a `scipy` KD-tree. Every station within 5 miles of the route is found in one vectorised query,
together with its mile marker along the route. The 6.7k stations live in memory as numpy
arrays, so a request never touches the database.

### 3. Where and how much to buy

This is the classic "gas station problem". For a fixed set of stations, the greedy strategy
is provably optimal (Khuller, Malekian & Mestre, *To fill or not to fill*, 2007):

- if a cheaper station is within a full tank's reach, buy just enough to get there;
- otherwise fill up and go to the cheapest station in range.

Used on its own, though, it stops wherever fuel is slightly cheaper. Seattle→Miami gets 23
stops, some for 0.01 gal. So each stop also carries a `stop_cost` (default $15, about 15
minutes of a driver's time), and the stops are chosen by a **dynamic program over (station,
fuel level)**. It runs in O(stations × 500), vectorised in a few milliseconds.

The DP works on a whole-mile grid, where it is exact. Rounding can blur a leg right at the
500-mile limit, so the DP runs twice: as is, and with a one-mile safety margin. Each stop
set it proposes, plus the pure greedy, is then priced exactly by the greedy, and the
cheapest feasible plan wins.

| Route | `stop_cost=0` | default (`$15`) |
|---|---|---|
| Chicago → Dallas (962 mi) | 7 stops, $281.77 | 2 stops, $292.12 |
| New York → Los Angeles (2,810 mi) | 17 stops, $859.97 | 7 stops, $869.06 |
| Seattle → Miami (3,303 mi) | 23 stops, $1,023.25 | 8 stops, $1,040.78 |

Both algorithms are checked against brute-force oracles in randomised tests:

- **The greedy:** it matches an exact integer DP.
- **The stop-cost plan:** it matches exhaustive search over station subsets whenever stations
  sit at whole-mile positions. At arbitrary positions it is never worse than the pure greedy
  and stays within $0.25 of the optimum. In a 2,200-route fuzz, 2,199 plans were exactly
  optimal and one was 4 cents off.

### Assumptions

- **The trip is fuel neutral.** The truck starts with a 5-gallon reserve and must finish with
  the same reserve. It therefore buys exactly the fuel it burns (distance ÷ 10 mpg), and the
  total is the full fuel cost of the trip. The reserve is larger if the first station is
  farther away. It is reduced if the tank can't carry it past the last station.
- **Each station is priced at its lowest listed price.** 678 stations appear more than once
  in the file with different prices.
- **Station positions are city-level**, which is why stations within 5 miles of the route count
  as on it. A detour to a station is not charged.
- **Canadian stations in the price list are kept.** Their prices are in USD per gallon like
  the rest, and some fastest routes between US cities cut through Canada (Detroit →
  Buffalo goes through Ontario). Start and finish must still be in the USA.
- The price list covers 48 states plus Canada, with **almost no California stations**. So
  starts in Northern California (e.g. San Francisco → Seattle) correctly return
  `no_feasible_fuel_plan`: the first station is more than 500 miles away.

## Performance

Measured on the public OSRM server (`meta.timings_ms` in every response):

| Request | Time |
|---|---|
| New route | 0.2–1.4 s, nearly all of it the OSRM call; our own processing is 10–40 ms |
| Repeated route (plan cache) | ~2 ms, 0 external calls |
| Same route, different `stop_cost` | Reuses the cached route: 0 routing calls |

The cache is per process by default. Set `REDIS_URL` to share it between workers.

## Development

```bash
pytest -q             # ~1,300 tests in ~8 s; OSRM and Nominatim are mocked
ruff format . && ruff check .
python manage.py build_geodata   # optional: rebuild data/ from the sources (~400 MB download)
```

Configuration is through environment variables (see `.env.example`):

- vehicle range and mpg;
- the reserve;
- `stop_cost`;
- the corridor width;
- the OSRM and Nominatim URLs (point them at self-hosted instances for production).

```
config/                  Django settings and URLs
fuel_planner/
  services/              framework-free core: geo, gazetteer, geocoding, routing, stations, optimizer, planner
  management/commands/   build_geodata (offline geocoding), load_stations (CSV → DB)
  views.py, serializers.py, exceptions.py, templates/   HTTP layer
  tests/
data/                    price list + derived geodata (committed)
postman/                 Postman collection with tests and a map visualizer
```

## Data sources

- **Fuel prices:** the provided OPIS price list (`data/fuel_prices.csv`).
- **US Census Bureau 2026 Gazetteer:** places and county subdivisions (public domain).
- **[GeoNames](https://www.geonames.org):** populated places (CC BY 4.0).
- **[Natural Earth](https://www.naturalearthdata.com):** 1:10m country outline (public domain).
- **Routing:** [OSRM](https://project-osrm.org).
- **Free-text geocoding:** [Nominatim](https://nominatim.org).
- **Map tiles:** © OpenStreetMap contributors.
