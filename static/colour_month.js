// Colour of your month: a calendar of tiles coloured by the album art of
// what you played in each, in one of several styles - the top album's
// colour, a blend, a woven mosaic, stripes or a gradient. Two layouts of the same plays - one tile
// per day, or one per hour of each day. Switching style or layout is just
// a redraw. Days and hours are in the browser's time zone.
const ColourMonth = (() => {
    const EXPORT_WIDTH = 2400;
    const LAYOUTS = {
        day: { label: "Day grid", description: "One tile per day, Monday to Sunday." },
        hour: { label: "Hour grid", description: "One row per day and one column per hour." },
    };
    const DEFAULT_LAYOUT = "day";
    const STYLES = {
        top: { label: "Top album", description: "The colour of the album you played most." },
        blend: { label: "Blend", description: "Every play's album colour mixed into one." },
        weave: { label: "Weave", description: "A mosaic of cells shared out between albums by how much you played them." },
        stripes: { label: "Stripes", description: "Each play as a band, in the order played, as wide as it lasted." },
        gradient: { label: "Gradient", description: "Flowing through the album colours in the order you played them." },
    };
    // Styles that fill a tile with one solid colour, so the day number can
    // just pick dark or light text instead of needing an outline.
    const SOLID_STYLES = ["blend", "top"];
    const DEFAULT_STYLE = "top";
    const TOP_TRACKS = 3;
    // Stripes: a play's band is as wide as it lasted (until the next play),
    // but a play counts for at most this long, so a break between sessions
    // doesn't swallow the tile, and the last play of a session gets a
    // typical track length.
    const STRIPE_MAX_SECONDS = 10 * 60;
    const STRIPE_LONE_SECONDS = 3.5 * 60;
    // Weave cells per side: day tiles are big enough for a 6x6 mosaic,
    // hour tiles for 3x3.
    const WEAVE_CELLS = { day: 6, hour: 3 };

    let monthStart;
    let daysInMonth = 30;
    let firstWeekday = 0; // Monday = 0
    let plays = [];
    let layoutKey = DEFAULT_LAYOUT;
    let styleKey = DEFAULT_STYLE;
    let dayTiles = [];
    let hourTiles = [];
    let theme = {};

    function cssVar(name) {
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    }

    function readTheme() {
        const bg = getComputedStyle(document.body).backgroundColor;
        theme = {
            bg,
            neutral: cssVar("--muted") || "#888",
            guide: cssVar("--border") || "#ccc",
            label: cssVar("--muted") || "#888",
            text: getComputedStyle(document.body).color,
        };
    }

    const toLinear = (c) => {
        c /= 255;
        return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    };
    // Clamped first: a boosted blend can land just outside the sRGB gamut.
    const fromLinear = (c) => {
        c = Math.min(Math.max(c, 0), 1);
        const v = c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055;
        return Math.round(v * 255);
    };

    // sRGB <-> OKLab (Björn Ottosson's matrices).
    function toOklab(hex) {
        const [r, g, b] = ListeningVisuals.hexToRgb(hex).map(toLinear);
        const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
        const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
        const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
        return [
            0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
        ];
    }

    function fromOklab([L, a, b]) {
        const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
        const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
        const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
        return [
            4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
        ].map(fromLinear);
    }

    // The play-weighted average of a tile's album colours, mixed in OKLab
    // rather than RGB so it doesn't slide towards muddy brown. Averaging
    // still pulls chroma down (hues partly cancel), so the result keeps its
    // hue but gets back the average chroma of the colours that went in.
    function blend(colouredPlays) {
        const colours = colouredPlays.map((play) => toOklab(play.c));
        const mean = [0, 1, 2].map((k) => colours.reduce((sum, c) => sum + c[k], 0) / colours.length);
        const meanChroma = colours.reduce((sum, c) => sum + Math.hypot(c[1], c[2]), 0) / colours.length;
        const chroma = Math.hypot(mean[1], mean[2]);
        if (chroma > 1e-4) {
            mean[1] *= meanChroma / chroma;
            mean[2] *= meanChroma / chroma;
        }
        const [r, g, b] = fromOklab(mean);
        return { rgb: [r, g, b], css: `rgb(${r}, ${g}, ${b})` };
    }

    function toColour(hex) {
        const [r, g, b] = ListeningVisuals.hexToRgb(hex);
        return { rgb: [r, g, b], css: hex };
    }

    // A tile's plays grouped by album (only albums with art), most played
    // first, ties going to whichever was played first in the tile.
    function rankAlbums(tilePlays) {
        const albums = new Map();
        tilePlays.forEach((play) => {
            if (!play.c) return;
            const key = `${play.a.toLowerCase()}\u0000${play.al.toLowerCase()}`;
            if (!albums.has(key)) albums.set(key, { name: play.al, artist: play.a, plays: 0, colour: toColour(play.c) });
            albums.get(key).plays += 1;
        });
        // Map keeps insertion (first-played) order, and the sort is stable.
        return [...albums.values()].sort((x, y) => y.plays - x.plays);
    }

    // A small seeded random number generator (mulberry32), so a tile's
    // weave is scattered the same way every time it's drawn.
    function seededRandom(seed) {
        return () => {
            seed = (seed + 0x6d2b79f5) | 0;
            let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
            t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
            return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
        };
    }

    // The weave: side x side cells shared out between the tile's albums in
    // proportion to their plays (largest remainders get the leftover
    // cells), then scattered. Returns each cell's album, row by row.
    function weaveCells(albums, side, seed) {
        const cellCount = side * side;
        const total = albums.reduce((sum, album) => sum + album.plays, 0);
        const quotas = albums.map((album) => (album.plays / total) * cellCount);
        const counts = quotas.map(Math.floor);
        const order = quotas.map((q, i) => [q - Math.floor(q), i]).sort((a, b) => b[0] - a[0]);
        for (let k = 0; counts.reduce((a, b) => a + b, 0) < cellCount; k++) counts[order[k % order.length][1]] += 1;
        const cells = counts.flatMap((count, i) => Array(count).fill(albums[i]));
        const random = seededRandom(seed);
        for (let i = cells.length - 1; i > 0; i--) {
            const j = Math.floor(random() * (i + 1));
            [cells[i], cells[j]] = [cells[j], cells[i]];
        }
        return cells;
    }

    // Stripes: [start, end, colour] bands across the tile (0 to 1), one per
    // play in the order played, sized by how long each lasted.
    function stripeBands(colouredPlays) {
        const lengths = colouredPlays.map((play, i) => {
            const next = colouredPlays[i + 1];
            const gap = next ? next.t - play.t : Infinity;
            return gap <= STRIPE_MAX_SECONDS ? gap : STRIPE_LONE_SECONDS;
        });
        const total = lengths.reduce((a, b) => a + b, 0);
        let start = 0;
        return colouredPlays.map((play, i) => {
            const band = [start / total, (start + lengths[i]) / total, play.c];
            start += lengths[i];
            return band;
        });
    }

    // Gradient: [position, colour] stops, one per play in the order played,
    // spaced by when it was played between the tile's first and last play.
    function gradientStops(colouredPlays) {
        const first = colouredPlays[0].t;
        const span = colouredPlays[colouredPlays.length - 1].t - first;
        return colouredPlays.map((play) => [span ? (play.t - first) / span : 0, play.c]);
    }

    // Each tile keeps its plays and their albums (most played first), plus
    // what the current style needs to draw it. label is the colour behind
    // the day number, for picking readable text; null when nothing in the
    // tile had album art.
    function makeTile(tilePlays, kind, seed) {
        const albums = rankAlbums(tilePlays);
        const coloured = tilePlays.filter((play) => play.c);
        const tile = { plays: tilePlays, albums, label: null };
        if (!coloured.length) return tile;
        if (styleKey === "stripes") {
            tile.bands = stripeBands(coloured);
            tile.label = toColour(coloured[0].c);
        } else if (styleKey === "weave") {
            tile.cells = weaveCells(albums, WEAVE_CELLS[kind], seed);
            tile.label = tile.cells[0].colour;
        } else if (styleKey === "gradient") {
            tile.stops = gradientStops(coloured);
            tile.label = toColour(coloured[0].c);
        } else if (styleKey === "top") {
            tile.label = albums[0].colour;
        } else {
            tile.blend = blend(coloured);
            tile.label = tile.blend;
        }
        return tile;
    }

    function buildTiles() {
        const byDay = Array.from({ length: daysInMonth }, () => []);
        const byHour = Array.from({ length: daysInMonth }, () => Array.from({ length: 24 }, () => []));
        plays.forEach((play) => {
            byDay[play.day].push(play);
            byHour[play.day][play.hour].push(play);
        });
        dayTiles = byDay.map((tilePlays, day) => makeTile(tilePlays, "day", day + 1));
        hourTiles = byHour.map((hours, day) =>
            hours.map((tilePlays, hour) => makeTile(tilePlays, "hour", (day + 1) * 100 + hour))
        );
    }

    function weekdayNames() {
        const monday = new Date(2024, 0, 1);
        return Array.from({ length: 7 }, (_, i) =>
            ListeningVisuals.addDays(monday, i).toLocaleDateString(undefined, { weekday: "short" })
        );
    }

    function geometry(width) {
        if (layoutKey === "day") {
            const header = width * 0.055;
            const gap = width * 0.012;
            const tile = (width - 6 * gap) / 7;
            const rows = Math.ceil((firstWeekday + daysInMonth) / 7);
            return { width, height: header + rows * tile + (rows - 1) * gap, header, gap, tileW: tile, tileH: tile, left: 0 };
        }
        const header = width * 0.05;
        const left = width * 0.06;
        const gap = Math.max(1.5, width * 0.0035);
        const tileW = (width - left - 23 * gap) / 24;
        const tileH = tileW * 0.8;
        return { width, height: header + daysInMonth * (tileH + gap) - gap, header, gap, tileW, tileH, left };
    }

    // [x, y] of a tile's top-left corner. Day grid: (day, -); hour grid:
    // (day, hour).
    function tileOrigin(geo, day, hour) {
        if (layoutKey === "day") {
            const index = firstWeekday + day;
            return [(index % 7) * (geo.tileW + geo.gap), geo.header + Math.floor(index / 7) * (geo.tileH + geo.gap)];
        }
        return [geo.left + hour * (geo.tileW + geo.gap), geo.header + day * (geo.tileH + geo.gap)];
    }

    function tileAt(geo, x, y) {
        if (layoutKey === "day") {
            const col = Math.floor(x / (geo.tileW + geo.gap));
            const row = Math.floor((y - geo.header) / (geo.tileH + geo.gap));
            const day = row * 7 + col - firstWeekday;
            if (y < geo.header || col < 0 || col > 6 || day < 0 || day >= daysInMonth) return null;
            return { day, hour: null, tile: dayTiles[day] };
        }
        const hour = Math.floor((x - geo.left) / (geo.tileW + geo.gap));
        const day = Math.floor((y - geo.header) / (geo.tileH + geo.gap));
        if (x < geo.left || y < geo.header || hour < 0 || hour > 23 || day < 0 || day >= daysInMonth) return null;
        return { day, hour, tile: hourTiles[day][hour] };
    }

    function roundedRect(ctx, x, y, w, h, r) {
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.arcTo(x + w, y, x + w, y + h, r);
        ctx.arcTo(x + w, y + h, x, y + h, r);
        ctx.arcTo(x, y + h, x, y, r);
        ctx.arcTo(x, y, x + w, y, r);
        ctx.closePath();
    }

    function drawTile(ctx, geo, tile, x, y) {
        const radius = Math.min(geo.tileW, geo.tileH) * 0.14;
        roundedRect(ctx, x, y, geo.tileW, geo.tileH, radius);
        if (!tile.plays.length) {
            // Nothing played: left blank, just a faint outline.
            ctx.globalAlpha = 0.7;
            ctx.strokeStyle = theme.guide;
            ctx.lineWidth = Math.max(1, geo.width / 900);
            ctx.stroke();
            ctx.globalAlpha = 1;
            return;
        }
        if (!tile.label) {
            // Played, but nothing with album art.
            ctx.fillStyle = theme.neutral;
            ctx.fill();
        } else {
            ctx.save();
            ctx.clip();
            if (styleKey === "stripes") drawStripes(ctx, geo, tile, x, y);
            else if (styleKey === "weave") drawWeave(ctx, geo, tile, x, y);
            else if (styleKey === "gradient") drawGradient(ctx, geo, tile, x, y);
            else if (styleKey === "top") drawSolid(ctx, geo, tile.label, x, y);
            else drawSolid(ctx, geo, tile.blend, x, y);
            ctx.restore();
        }
        // A faint edge, so a black tile on the dark theme (or a white one
        // on the light theme) doesn't vanish into the background.
        roundedRect(ctx, x, y, geo.tileW, geo.tileH, radius);
        ctx.globalAlpha = 0.7;
        ctx.strokeStyle = theme.guide;
        ctx.lineWidth = Math.max(1, geo.width / 900);
        ctx.stroke();
        ctx.globalAlpha = 1;
    }

    // The tile's plays side by side as solid bands, in the order played,
    // each as wide as it lasted.
    function drawStripes(ctx, geo, tile, x, y) {
        tile.bands.forEach(([start, end, colour]) => {
            ctx.fillStyle = colour;
            // A hair of overlap so no background shows between bands.
            ctx.fillRect(x + start * geo.tileW, y, (end - start) * geo.tileW + 0.5, geo.tileH);
        });
    }

    function drawWeave(ctx, geo, tile, x, y) {
        const cellsPerSide = Math.round(Math.sqrt(tile.cells.length));
        const w = geo.tileW / cellsPerSide;
        const h = geo.tileH / cellsPerSide;
        tile.cells.forEach((album, i) => {
            ctx.fillStyle = album.colour.css;
            // A hair of overlap so no background shows between cells.
            ctx.fillRect(x + (i % cellsPerSide) * w, y + Math.floor(i / cellsPerSide) * h, w + 0.5, h + 0.5);
        });
    }

    // Left to right through the tile's album colours in the order they
    // were played, spaced by when, from its first play to its last.
    function drawGradient(ctx, geo, tile, x, y) {
        if (tile.stops.length === 1) {
            ctx.fillStyle = tile.stops[0][1];
        } else {
            const gradient = ctx.createLinearGradient(x, 0, x + geo.tileW, 0);
            tile.stops.forEach(([position, colour]) => gradient.addColorStop(position, colour));
            ctx.fillStyle = gradient;
        }
        ctx.fillRect(x, y, geo.tileW, geo.tileH);
    }

    function drawSolid(ctx, geo, colour, x, y) {
        ctx.fillStyle = colour.css;
        ctx.fillRect(x, y, geo.tileW, geo.tileH);
    }

    // Text that reads on a tile: dark on light tiles, light on dark ones.
    function textColourOn(tile) {
        if (!tile.label) return theme.label;
        const [r, g, b] = tile.label.rgb;
        return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 > 0.55 ? "rgba(0, 0, 0, 0.6)" : "rgba(255, 255, 255, 0.75)";
    }

    function draw(ctx, geo) {
        ctx.fillStyle = theme.bg;
        ctx.fillRect(0, 0, geo.width, geo.height);
        ctx.textBaseline = "middle";

        if (layoutKey === "day") {
            ctx.font = `${Math.round(geo.width * 0.024)}px sans-serif`;
            ctx.textAlign = "center";
            ctx.fillStyle = theme.label;
            weekdayNames().forEach((name, i) => ctx.fillText(name, i * (geo.tileW + geo.gap) + geo.tileW / 2, geo.header * 0.45));
            const numberSize = Math.round(geo.tileW * 0.17);
            dayTiles.forEach((tile, day) => {
                const [x, y] = tileOrigin(geo, day);
                drawTile(ctx, geo, tile, x, y);
                ctx.font = `${numberSize}px sans-serif`;
                ctx.textAlign = "left";
                const [nx, ny] = [x + numberSize * 0.6, y + numberSize * 0.9];
                if (!SOLID_STYLES.includes(styleKey) && tile.label) {
                    // Busy tiles: white text with a dark outline reads on
                    // any mix of colours.
                    ctx.lineJoin = "round";
                    ctx.lineWidth = Math.max(2, numberSize * 0.25);
                    ctx.strokeStyle = "rgba(0, 0, 0, 0.55)";
                    ctx.strokeText(String(day + 1), nx, ny);
                    ctx.fillStyle = "rgba(255, 255, 255, 0.95)";
                } else {
                    ctx.fillStyle = tile.plays.length ? textColourOn(tile) : theme.label;
                }
                ctx.fillText(String(day + 1), nx, ny);
            });
            return;
        }

        const fontSize = Math.round(geo.width * 0.02);
        ctx.font = `${fontSize}px sans-serif`;
        ctx.fillStyle = theme.label;
        ctx.textAlign = "center";
        [0, 6, 12, 18].forEach((hour) => {
            ctx.fillText(String(hour).padStart(2, "0"), geo.left + hour * (geo.tileW + geo.gap) + geo.tileW / 2, geo.header * 0.45);
        });
        ctx.textAlign = "right";
        const step = Math.max(1, Math.ceil((fontSize * 1.3) / (geo.tileH + geo.gap)));
        for (let day = 0; day < daysInMonth; day += step) {
            const [, y] = tileOrigin(geo, day, 0);
            ctx.fillText(String(day + 1), geo.left - fontSize * 0.5, y + geo.tileH / 2);
        }
        hourTiles.forEach((hours, day) =>
            hours.forEach((tile, hour) => {
                const [x, y] = tileOrigin(geo, day, hour);
                drawTile(ctx, geo, tile, x, y);
            })
        );
    }

    function sizeCanvas(canvas, geo) {
        const ratio = window.devicePixelRatio || 1;
        canvas.width = Math.round(geo.width * ratio);
        canvas.height = Math.round(geo.height * ratio);
        const ctx = canvas.getContext("2d");
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        return ctx;
    }

    function render() {
        readTheme();
        const canvas = document.getElementById("month-canvas");
        const geo = geometry(canvas.parentElement.clientWidth);
        draw(sizeCanvas(canvas, geo), geo);
        sizeCanvas(document.getElementById("month-overlay"), geo);
        hideTooltip();
    }

    function hideTooltip() {
        document.getElementById("month-tooltip").style.display = "none";
        const overlay = document.getElementById("month-overlay");
        overlay.getContext("2d").clearRect(0, 0, overlay.width, overlay.height);
    }

    function describeTile(day, hour) {
        const date = new Date(monthStart.getFullYear(), monthStart.getMonth(), day + 1);
        const label = date.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
        if (hour === null) return label;
        const pad = (n) => String(n).padStart(2, "0");
        return `${label}, ${pad(hour)}:00–${pad((hour + 1) % 24)}:00`;
    }

    function onPointer(event) {
        const canvas = document.getElementById("month-canvas");
        const rect = canvas.getBoundingClientRect();
        const geo = geometry(rect.width);
        const x = event.clientX - rect.left;
        const y = event.clientY - rect.top;
        const hit = tileAt(geo, x, y);
        if (!hit) {
            hideTooltip();
            return;
        }

        const ctx = document.getElementById("month-overlay").getContext("2d");
        ctx.clearRect(0, 0, geo.width, geo.height);
        const [tx, ty] = tileOrigin(geo, hit.day, hit.hour);
        roundedRect(ctx, tx - 1.5, ty - 1.5, geo.tileW + 3, geo.tileH + 3, Math.min(geo.tileW, geo.tileH) * 0.16);
        ctx.strokeStyle = theme.text;
        ctx.lineWidth = 2;
        ctx.stroke();

        const tooltip = document.getElementById("month-tooltip");
        tooltip.replaceChildren();
        const when = document.createElement("div");
        when.textContent = describeTile(hit.day, hit.hour);
        const count = document.createElement("div");
        const n = hit.tile.plays.length;
        count.className = "when";
        count.textContent = n ? `${n} play${n === 1 ? "" : "s"}` : "Nothing played";
        tooltip.append(when, count);

        // The albums behind the tile's colours, most played first.
        hit.tile.albums.slice(0, 3).forEach((album) => {
            const line = document.createElement("div");
            line.className = "when";
            line.textContent = `${album.name} – ${album.artist} (${album.plays} play${album.plays === 1 ? "" : "s"})`;
            tooltip.append(line);
        });

        // The tile's most-played tracks.
        const tally = new Map();
        hit.tile.plays.forEach((play) => {
            const key = `${play.a}\u0000${play.n}`;
            tally.set(key, (tally.get(key) || 0) + 1);
        });
        [...tally.entries()]
            .sort((a, b) => b[1] - a[1])
            .slice(0, TOP_TRACKS)
            .forEach(([key, times]) => {
                const [artist, name] = key.split("\u0000");
                const line = document.createElement("div");
                line.textContent = `${name} – ${artist}${times > 1 ? ` (×${times})` : ""}`;
                tooltip.append(line);
            });

        tooltip.style.display = "block";
        const left = x + 14 + tooltip.offsetWidth > geo.width ? x - 14 - tooltip.offsetWidth : x + 14;
        tooltip.style.left = `${Math.max(0, left)}px`;
        tooltip.style.top = `${Math.max(0, Math.min(y + 14, geo.height - tooltip.offsetHeight))}px`;
    }

    function monthKey(date) {
        return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
    }

    function remember() {
        ListeningVisuals.remember("month", { range: "month", month: monthKey(monthStart), layout: layoutKey, style: styleKey });
    }

    function download() {
        const geo = geometry(EXPORT_WIDTH);
        const canvas = document.createElement("canvas");
        canvas.width = geo.width;
        canvas.height = Math.round(geo.height);
        draw(canvas.getContext("2d"), geo);
        canvas.toBlob((blob) => {
            const link = document.createElement("a");
            link.href = URL.createObjectURL(blob);
            link.download = `colour-of-${monthKey(monthStart)}-${styleKey}-${layoutKey}-grid.png`;
            link.click();
            setTimeout(() => URL.revokeObjectURL(link.href), 1000);
        }, "image/png");
    }

    // Builds a row of pill buttons (one per option) into picker, calling
    // onSelect(key) on click.
    function buildPicker(picker, options, onSelect) {
        Object.entries(options).forEach(([key, config]) => {
            const button = document.createElement("button");
            button.type = "button";
            button.dataset.key = key;
            button.textContent = config.label;
            button.addEventListener("click", () => onSelect(key));
            picker.appendChild(button);
        });
    }

    function markSelected(picker, key) {
        picker.querySelectorAll("button").forEach((button) => {
            button.setAttribute("aria-pressed", String(button.dataset.key === key));
        });
    }

    function updateDescription() {
        document.getElementById("month-description").textContent =
            `${LAYOUTS[layoutKey].description} ${STYLES[styleKey].description}`;
    }

    function selectLayout(key) {
        layoutKey = key;
        markSelected(document.getElementById("month-layout"), key);
        updateDescription();
        remember();
        render();
    }

    // A new style changes what each tile keeps, so the tiles are rebuilt.
    function selectStyle(key) {
        styleKey = key;
        markSelected(document.getElementById("month-style"), key);
        buildTiles();
        updateDescription();
        remember();
        render();
    }

    function setUpControls() {
        buildPicker(document.getElementById("month-layout"), LAYOUTS, selectLayout);
        buildPicker(document.getElementById("month-style"), STYLES, selectStyle);

        document.getElementById("month-label").textContent = monthStart.toLocaleDateString(undefined, {
            month: "long",
            year: "numeric",
        });
        const goTo = (offset) => {
            const target = new Date(monthStart.getFullYear(), monthStart.getMonth() + offset, 1);
            ListeningVisuals.open("month", { range: "month", month: monthKey(target) });
        };
        document.getElementById("month-prev").addEventListener("click", () => goTo(-1));
        const next = document.getElementById("month-next");
        const thisMonth = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
        next.disabled = monthStart >= thisMonth;
        next.addEventListener("click", () => goTo(1));
    }

    function init(options) {
        monthStart = ListeningVisuals.startOfDay(new Date(options.fromTs * 1000));
        monthStart = new Date(monthStart.getFullYear(), monthStart.getMonth(), 1);
        daysInMonth = new Date(monthStart.getFullYear(), monthStart.getMonth() + 1, 0).getDate();
        firstWeekday = (monthStart.getDay() + 6) % 7;

        plays = options.plays
            .map((play) => {
                const date = new Date(play.t * 1000);
                return { ...play, day: date.getDate() - 1, hour: date.getHours(), month: date.getMonth() };
            })
            .filter((play) => play.month === monthStart.getMonth());

        const label = monthStart.toLocaleDateString(undefined, { month: "long", year: "numeric" });
        const noArt = plays.filter((play) => !play.c).length;
        document.getElementById("month-summary").textContent = plays.length
            ? `${plays.length.toLocaleString()} plays in ${label}` +
              (noArt ? ` · ${noArt.toLocaleString()} without album art, left out of the colours` : "")
            : `No plays in ${label}.`;

        setUpControls();
        const saved = ListeningVisuals.savedSettings("month");
        layoutKey = LAYOUTS[saved.layout] ? saved.layout : DEFAULT_LAYOUT;
        markSelected(document.getElementById("month-layout"), layoutKey);
        selectStyle(STYLES[saved.style] ? saved.style : DEFAULT_STYLE);

        let pending = false;
        window.addEventListener("resize", () => {
            if (pending) return;
            pending = true;
            requestAnimationFrame(() => {
                pending = false;
                render();
            });
        });
        window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);

        const canvas = document.getElementById("month-canvas");
        canvas.addEventListener("pointermove", onPointer);
        canvas.addEventListener("pointerdown", onPointer);
        canvas.addEventListener("pointerleave", hideTooltip);
        document.getElementById("month-download").addEventListener("click", download);
    }

    return { init };
})();
