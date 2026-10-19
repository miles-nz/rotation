// The artist river: a streamgraph of plays per artist over time. Each of
// the top artists is a band whose thickness is their plays that week (or
// month, for ranges over two years). Optionally, everyone else shares one
// faint band along the top edge, so the river's overall width is all your
// listening - off by default, since for wide-ranging listening it can be
// most of the river and squeeze the artists into a thin strip.
// Plays are grouped into weeks/months here, in the browser's time zone, so
// changing the artist count is just a redraw. Drawn on a canvas, with the
// stacking ("wiggle", which keeps the river flowing rather than sitting on
// a flat baseline) and the smooth curves done by hand.
const ArtistRiver = (() => {
    const EXPORT_WIDTH = 2400;
    const MONTHLY_AFTER_DAYS = 2 * 365;
    const ARTIST_COUNTS = [5, 10, 15, 20, 30];
    const DEFAULT_ARTIST_COUNT = 15;

    let times = [];
    let artistIds = [];
    let artists = [];
    let buckets = []; // start Date of each week/month
    let monthly = false;
    let firstDay;
    let lastDay;
    let settings = {};
    let artistCount = DEFAULT_ARTIST_COUNT;
    let includeOthers = false;
    let river = null; // {series: [{name, key, values, other}], lower, upper, maxY, minY}
    let highlighted = -1;
    let theme = {};

    function cssVar(name) {
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    }

    function readTheme() {
        const bg = getComputedStyle(document.body).backgroundColor;
        const rgb = (bg.match(/\d+/g) || [255, 255, 255]).slice(0, 3).map(Number);
        theme = {
            bg,
            dark: (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255 < 0.5,
            neutral: cssVar("--muted") || "#888",
            guide: cssVar("--border") || "#ccc",
            label: cssVar("--muted") || "#888",
            text: getComputedStyle(document.body).color,
        };
    }

    // An artist without album art gets a muted colour from their name, so
    // it's at least the same whatever the range.
    function nameColour(key) {
        let hash = 2166136261;
        for (let i = 0; i < key.length; i++) {
            hash ^= key.charCodeAt(i);
            hash = Math.imul(hash, 16777619);
        }
        const hue = (hash >>> 0) % 360;
        return `hsl(${hue}, 38%, ${theme.dark ? 60 : 62}%)`;
    }

    // A top artist's band takes the most vivid colour of their most-played
    // album of all time, kept in a lightness band that reads on the
    // background and under the outlined labels.
    function albumColour(hex) {
        let [h, s, l] = ListeningVisuals.rgbToHsl(ListeningVisuals.hexToRgb(hex));
        l = theme.dark ? Math.min(Math.max(l, 0.38), 0.75) : Math.min(Math.max(l, 0.32), 0.7);
        const [r, g, b] = ListeningVisuals.hslToRgb([h, s, l]);
        return `rgb(${r}, ${g}, ${b})`;
    }

    function bandColour(series) {
        if (series.other) return theme.neutral;
        return series.colour ? albumColour(series.colour) : nameColour(series.key);
    }

    // Weeks start on Monday; months on the 1st.
    function bucketStart(date) {
        const day = ListeningVisuals.startOfDay(date);
        if (monthly) return new Date(day.getFullYear(), day.getMonth(), 1);
        return ListeningVisuals.addDays(day, -((day.getDay() + 6) % 7));
    }

    function nextBucket(start) {
        if (monthly) return new Date(start.getFullYear(), start.getMonth() + 1, 1);
        return ListeningVisuals.addDays(start, 7);
    }

    function bucketIndex(date) {
        const first = buckets[0];
        if (monthly) return (date.getFullYear() - first.getFullYear()) * 12 + date.getMonth() - first.getMonth();
        const days = Math.round((ListeningVisuals.startOfDay(date) - first) / 864e5);
        return Math.floor(days / 7);
    }

    // d3's "inside-out" order: artists peaking near the middle of the range
    // sit in the middle of the river, earlier and later peaks towards the
    // edges, balancing the two sides by size - so bands don't cut across
    // each other. "Everyone else" always goes on the outside edge.
    function insideOut(seriesList) {
        const peakOf = (s) => s.values.indexOf(Math.max(...s.values));
        const sorted = [...seriesList].sort((a, b) => peakOf(a) - peakOf(b));
        const tops = [];
        const bottoms = [];
        let topSum = 0;
        let bottomSum = 0;
        sorted.forEach((s) => {
            const total = s.values.reduce((a, b) => a + b, 0);
            if (topSum < bottomSum) {
                tops.push(s);
                topSum += total;
            } else {
                bottoms.push(s);
                bottomSum += total;
            }
        });
        return bottoms.reverse().concat(tops);
    }

    // The top artistCount artists by plays in the range, plus everyone
    // else, stacked with d3's wiggle offset (Byron & Wattenberg): the
    // baseline moves to minimise how much the bands' slopes change, which
    // is what makes a streamgraph flow.
    function buildRiver() {
        const totals = new Map();
        artistIds.forEach((id) => totals.set(id, (totals.get(id) || 0) + 1));
        const top = [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, artistCount).map(([id]) => id);
        const topIndex = new Map(top.map((id, i) => [id, i]));

        const m = buckets.length;
        const values = top.map(() => new Array(m).fill(0));
        const other = new Array(m).fill(0);
        times.forEach((t, i) => {
            const j = bucketIndex(new Date(t * 1000));
            if (j < 0 || j >= m) return;
            const row = topIndex.get(artistIds[i]);
            if (row === undefined) other[j] += 1;
            else values[row][j] += 1;
        });

        const series = insideOut(
            top.map((id, i) => ({
                name: artists[id].name,
                key: artists[id].key,
                colour: artists[id].colour,
                values: values[i],
                total: totals.get(id),
            }))
        );
        if (includeOthers && top.length < totals.size) {
            series.push({ name: "Everyone else", key: "", values: other, other: true, total: other.reduce((a, b) => a + b, 0) });
        }

        const n = series.length;
        const baseline = new Array(m).fill(0);
        for (let j = 1; j < m; j++) {
            let sum = 0;
            let weighted = 0;
            for (let i = 0; i < n; i++) {
                const v = series[i].values[j];
                let slope = (v - series[i].values[j - 1]) / 2;
                for (let k = 0; k < i; k++) slope += series[k].values[j] - series[k].values[j - 1];
                sum += v;
                weighted += slope * v;
            }
            baseline[j] = baseline[j - 1] - (sum ? weighted / sum : 0);
        }
        const lower = [];
        const upper = [];
        let minY = Infinity;
        let maxY = -Infinity;
        series.forEach((s, i) => {
            lower[i] = s.values.map((_, j) => (i ? upper[i - 1][j] : baseline[j]));
            upper[i] = s.values.map((v, j) => lower[i][j] + v);
        });
        for (let j = 0; j < m; j++) {
            minY = Math.min(minY, n ? lower[0][j] : 0);
            maxY = Math.max(maxY, n ? upper[n - 1][j] : 0);
        }
        river = { series, lower, upper, minY, maxY, otherCount: totals.size - top.length, artistTotal: totals.size };
    }

    function geometry(width) {
        const height = width * 0.62;
        const left = width * 0.02;
        const right = width * 0.02;
        const top = width * 0.03;
        const bottom = width * 0.07;
        const plotWidth = width - left - right;
        const plotHeight = height - top - bottom;
        const span = river.maxY - river.minY || 1;
        const m = buckets.length;
        return {
            width,
            height,
            left,
            top,
            plotWidth,
            plotHeight,
            x: (j) => left + (m > 1 ? (j / (m - 1)) * plotWidth : plotWidth / 2),
            y: (value) => top + plotHeight - ((value - river.minY) / span) * plotHeight,
        };
    }

    // d3's curveBasis: a B-spline through the points, smooth and never
    // overshooting them (so bands can't cross). continueLine joins it onto
    // the current path instead of starting a new one.
    function traceBasis(ctx, points, continueLine) {
        let [x0, y0, x1, y1] = [NaN, NaN, NaN, NaN];
        const curve = (x, y) =>
            ctx.bezierCurveTo(
                (2 * x0 + x1) / 3,
                (2 * y0 + y1) / 3,
                (x0 + 2 * x1) / 3,
                (y0 + 2 * y1) / 3,
                (x0 + 4 * x1 + x) / 6,
                (y0 + 4 * y1 + y) / 6
            );
        points.forEach(([x, y], index) => {
            if (index === 0) {
                if (continueLine) ctx.lineTo(x, y);
                else ctx.moveTo(x, y);
            } else if (index === 2) {
                ctx.lineTo((5 * x0 + x1) / 6, (5 * y0 + y1) / 6);
                curve(x, y);
            } else if (index > 2) {
                curve(x, y);
            }
            [x0, y0, x1, y1] = [x1, y1, x, y];
        });
        if (points.length > 2) curve(x1, y1);
        if (points.length > 1) ctx.lineTo(x1, y1);
    }

    function traceBand(ctx, i, geo) {
        const m = buckets.length;
        const upperPoints = river.upper[i].map((v, j) => [geo.x(j), geo.y(v)]);
        const lowerPoints = river.lower[i].map((v, j) => [geo.x(j), geo.y(v)]).reverse();
        ctx.beginPath();
        if (m === 1) {
            // A single week or month: a short bar rather than a curve.
            const w = geo.plotWidth * 0.1;
            ctx.rect(geo.x(0) - w / 2, upperPoints[0][1], w, lowerPoints[0][1] - upperPoints[0][1]);
            return;
        }
        traceBasis(ctx, upperPoints, false);
        traceBasis(ctx, lowerPoints, true);
        ctx.closePath();
    }

    // Faint lines and labels at the start of each month (week buckets) or
    // year (month buckets), at an even step - every month, or every 2nd,
    // 3rd... - chosen so the labels never crowd.
    function drawAxis(ctx, geo) {
        const fontSize = Math.round(geo.width * 0.02);
        ctx.font = `${fontSize}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        // [bucket index, the 1st of the month it contains] for each bucket
        // that contains a 1st of the month.
        const starts = [];
        buckets.forEach((start, j) => {
            const end = nextBucket(start);
            if (monthly) starts.push([j, start]);
            else if (start.getDate() === 1) starts.push([j, start]);
            else if (end.getMonth() !== start.getMonth() && end.getDate() !== 1) {
                starts.push([j, new Date(end.getFullYear(), end.getMonth(), 1)]);
            }
        });
        const yearly = monthly;
        const ticks = starts.filter(([, date]) => !yearly || date.getMonth() === 0);
        const steps = yearly ? [1, 2, 5, 10] : [1, 2, 3, 6, 12];
        const minGap = fontSize * 4.5;
        const spacing = ticks.length > 1 ? (geo.x(ticks[ticks.length - 1][0]) - geo.x(ticks[0][0])) / (ticks.length - 1) : Infinity;
        const step = steps.find((n) => spacing * n >= minGap) || steps[steps.length - 1];
        const shown = ticks.filter(([, date]) =>
            yearly ? date.getFullYear() % step === 0 : (date.getFullYear() * 12 + date.getMonth()) % step === 0
        );

        ctx.strokeStyle = theme.guide;
        ctx.lineWidth = Math.max(1, geo.width / 900);
        shown.forEach(([j, date]) => {
            const x = geo.x(j);
            ctx.globalAlpha = 0.6;
            ctx.beginPath();
            ctx.moveTo(x, geo.top);
            ctx.lineTo(x, geo.top + geo.plotHeight);
            ctx.stroke();
            ctx.globalAlpha = 1;
            let label = String(date.getFullYear());
            if (!yearly) {
                label = date.toLocaleDateString(undefined, { month: "short" });
                if (date.getMonth() === 0) label += ` ${date.getFullYear()}`;
            }
            ctx.fillStyle = theme.label;
            ctx.fillText(label, Math.min(Math.max(x, fontSize * 2), geo.width - fontSize * 2), geo.top + geo.plotHeight + geo.width * 0.035);
        });
    }

    // Artist names at their band's peak, where the band is tall enough
    // there to hold the text. Weekly listening is spiky, so a band is
    // rarely that tall across a whole name - the text is allowed to spill
    // sideways over neighbouring bands, with an outline in the background
    // colour to keep it legible. Thickness is measured on the smoothed
    // curve that's drawn, not the raw counts. Names that would overlap one
    // already placed are skipped.
    function drawLabels(ctx, geo) {
        const fontSize = Math.round(geo.width * 0.019);
        ctx.font = `600 ${fontSize}px sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.lineJoin = "round";
        ctx.lineWidth = Math.max(2, fontSize * 0.28);
        ctx.strokeStyle = theme.bg;
        ctx.fillStyle = theme.text;
        const m = buckets.length;
        const placed = [];
        // A B-spline passes through (prev + 4 * this + next) / 6.
        const smoothed = (edge, j) =>
            m > 2 ? (edge[Math.max(j - 1, 0)] + 4 * edge[j] + edge[Math.min(j + 1, m - 1)]) / 6 : edge[j];
        const thickness = (i, j) => geo.y(smoothed(river.lower[i], j)) - geo.y(smoothed(river.upper[i], j));
        river.series.forEach((s, i) => {
            let best = 0;
            for (let j = 1; j < m; j++) if (thickness(i, j) > thickness(i, best)) best = j;
            if (thickness(i, best) < fontSize * 1.2) return;
            const textWidth = ctx.measureText(s.name).width;
            const x = Math.min(Math.max(geo.x(best), geo.left + textWidth / 2), geo.left + geo.plotWidth - textWidth / 2);
            const y = geo.y((smoothed(river.lower[i], best) + smoothed(river.upper[i], best)) / 2);
            const box = [x - textWidth / 2 - 4, y - fontSize / 2 - 2, x + textWidth / 2 + 4, y + fontSize / 2 + 2];
            if (placed.some((p) => box[0] < p[2] && box[2] > p[0] && box[1] < p[3] && box[3] > p[1])) return;
            placed.push(box);
            ctx.strokeText(s.name, x, y);
            ctx.fillText(s.name, x, y);
        });
    }

    function draw(ctx, geo) {
        ctx.fillStyle = theme.bg;
        ctx.fillRect(0, 0, geo.width, geo.height);
        drawAxis(ctx, geo);
        river.series.forEach((s, i) => {
            // Everyone else is kept faint, so it shows the river's full
            // width without drowning out the artists.
            const base = s.other ? 0.18 : 0.95;
            ctx.globalAlpha = highlighted >= 0 && highlighted !== i ? base * 0.4 : s.other && highlighted === i ? 0.35 : base;
            ctx.fillStyle = bandColour(s);
            traceBand(ctx, i, geo);
            ctx.fill();
        });
        ctx.globalAlpha = 1;
        if (highlighted >= 0) {
            ctx.strokeStyle = theme.text;
            ctx.lineWidth = Math.max(1.5, geo.width / 500);
            traceBand(ctx, highlighted, geo);
            ctx.stroke();
        }
        drawLabels(ctx, geo);
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
        const canvas = document.getElementById("river-canvas");
        const geo = geometry(canvas.parentElement.clientWidth);
        draw(sizeCanvas(canvas, geo), geo);
        sizeCanvas(document.getElementById("river-overlay"), geo);
    }

    // Which band (and week/month) is under a point, using a straight-line
    // approximation of the curves between buckets.
    function bandAt(x, y, geo) {
        const m = buckets.length;
        if (!m || x < geo.left || x > geo.left + geo.plotWidth) return null;
        const position = m > 1 ? ((x - geo.left) / geo.plotWidth) * (m - 1) : 0;
        const j0 = Math.floor(position);
        const j1 = Math.min(j0 + 1, m - 1);
        const f = position - j0;
        const at = (edge) => edge[j0] + (edge[j1] - edge[j0]) * f;
        for (let i = 0; i < river.series.length; i++) {
            const topY = geo.y(at(river.upper[i]));
            const bottomY = geo.y(at(river.lower[i]));
            if (y >= topY && y <= bottomY) return { band: i, bucket: Math.round(position) };
        }
        return null;
    }

    function describeBucket(start) {
        if (monthly) return start.toLocaleDateString(undefined, { month: "long", year: "numeric" });
        return `week of ${start.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" })}`;
    }

    function hideHover() {
        document.getElementById("river-tooltip").style.display = "none";
        const overlay = document.getElementById("river-overlay");
        overlay.getContext("2d").clearRect(0, 0, overlay.width, overlay.height);
        if (highlighted !== -1) {
            highlighted = -1;
            render();
        }
    }

    function onPointer(event) {
        const canvas = document.getElementById("river-canvas");
        const rect = canvas.getBoundingClientRect();
        const geo = geometry(rect.width);
        const x = event.clientX - rect.left;
        const y = event.clientY - rect.top;
        const hit = bandAt(x, y, geo);
        if (!hit) {
            hideHover();
            return;
        }
        if (hit.band !== highlighted) {
            highlighted = hit.band;
            render();
        }
        // A faint line marking the week/month being described.
        const ctx = document.getElementById("river-overlay").getContext("2d");
        ctx.clearRect(0, 0, geo.width, geo.height);
        ctx.strokeStyle = theme.text;
        ctx.globalAlpha = 0.5;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(geo.x(hit.bucket), geo.top);
        ctx.lineTo(geo.x(hit.bucket), geo.top + geo.plotHeight);
        ctx.stroke();
        ctx.globalAlpha = 1;

        const s = river.series[hit.band];
        const plays = s.values[hit.bucket];
        const tooltip = document.getElementById("river-tooltip");
        tooltip.replaceChildren();
        const name = document.createElement("div");
        name.textContent = s.other ? `Everyone else (${river.otherCount.toLocaleString()} artists)` : s.name;
        const count = document.createElement("div");
        count.textContent = `${plays.toLocaleString()} play${plays === 1 ? "" : "s"}, ${describeBucket(buckets[hit.bucket])}`;
        const total = document.createElement("div");
        total.className = "when";
        total.textContent = `${s.total.toLocaleString()} plays in this range`;
        tooltip.append(name, count, total);
        tooltip.style.display = "block";
        const left = x + 14 + tooltip.offsetWidth > geo.width ? x - 14 - tooltip.offsetWidth : x + 14;
        tooltip.style.left = `${Math.max(0, left)}px`;
        tooltip.style.top = `${Math.max(0, Math.min(y + 14, geo.height - tooltip.offsetHeight))}px`;
    }

    function download() {
        const saved = highlighted;
        highlighted = -1;
        const geo = geometry(EXPORT_WIDTH);
        const canvas = document.createElement("canvas");
        canvas.width = geo.width;
        canvas.height = Math.round(geo.height);
        draw(canvas.getContext("2d"), geo);
        highlighted = saved;
        const from = ListeningVisuals.formatDate(firstDay);
        const to = ListeningVisuals.formatDate(lastDay);
        canvas.toBlob((blob) => {
            const link = document.createElement("a");
            link.href = URL.createObjectURL(blob);
            link.download = `artist-river-${from}-to-${to}.png`;
            link.click();
            setTimeout(() => URL.revokeObjectURL(link.href), 1000);
        }, "image/png");
    }

    function remember() {
        ListeningVisuals.remember("river", {
            range: settings.range,
            start: settings.range === "custom" ? ListeningVisuals.formatDate(firstDay) : "",
            end: settings.range === "custom" ? ListeningVisuals.formatDate(lastDay) : "",
            artists: artistCount,
            others: includeOthers,
        });
    }

    function describeRange() {
        const opts = { day: "numeric", month: "short", year: "numeric" };
        return `${firstDay.toLocaleDateString(undefined, opts)} – ${lastDay.toLocaleDateString(undefined, opts)}`;
    }

    function updateSummary() {
        const summary = document.getElementById("river-summary");
        if (!times.length) {
            summary.textContent = `No plays between ${describeRange()}.`;
            return;
        }
        const topPlays = river.series.filter((s) => !s.other).reduce((a, s) => a + s.total, 0);
        const unit = monthly ? "month" : "week";
        summary.textContent =
            `${times.length.toLocaleString()} plays by ${river.artistTotal.toLocaleString()} artists, ` +
            `${describeRange()}, by ${unit}. ` +
            `Your top ${Math.min(artistCount, river.artistTotal)} account for ${Math.round((topPlays / times.length) * 100)}%.`;
    }

    function setUpControls() {
        const form = document.getElementById("river-controls");
        const select = document.getElementById("river-range");
        const start = document.getElementById("river-start");
        const end = document.getElementById("river-end");
        const error = document.getElementById("river-range-error");
        const count = document.getElementById("river-artists");
        const today = ListeningVisuals.formatDate(new Date());

        select.value = settings.range;
        if (!select.value) select.value = "12m";
        start.value = ListeningVisuals.formatDate(firstDay);
        end.value = ListeningVisuals.formatDate(lastDay);
        start.max = today;
        end.max = today;
        form.classList.toggle("custom", select.value === "custom");

        ARTIST_COUNTS.forEach((n) => {
            const option = document.createElement("option");
            option.value = String(n);
            option.textContent = `Top ${n} artists`;
            count.appendChild(option);
        });
        count.value = String(artistCount);
        const others = document.getElementById("river-others");
        others.checked = includeOthers;

        select.addEventListener("change", () => {
            if (select.value === "custom") {
                form.classList.add("custom");
                start.focus();
            } else {
                ListeningVisuals.open("river", { range: select.value });
            }
        });
        const redraw = () => {
            remember();
            buildRiver();
            updateSummary();
            highlighted = -1;
            render();
        };
        count.addEventListener("change", () => {
            artistCount = Number(count.value);
            redraw();
        });
        others.addEventListener("change", () => {
            includeOthers = others.checked;
            redraw();
        });
        form.addEventListener("submit", (event) => {
            event.preventDefault();
            const first = ListeningVisuals.parseDate(start.value);
            const last = ListeningVisuals.parseDate(end.value);
            let message = "";
            if (!first || !last) message = "Pick a start and end date.";
            else if (first > last) message = "The start date needs to be before the end date.";
            error.textContent = message;
            error.style.display = message ? "block" : "none";
            if (!message) ListeningVisuals.open("river", { range: "custom", start: start.value, end: end.value });
        });
    }

    function init(options) {
        settings = options.settings;
        times = options.times;
        artistIds = options.artistIds;
        artists = options.artists;
        const saved = ListeningVisuals.savedSettings("river");
        artistCount = ARTIST_COUNTS.includes(Number(saved.artists)) ? Number(saved.artists) : DEFAULT_ARTIST_COUNT;
        includeOthers = saved.others === true;

        // "All time" asks from 0, so it starts from the first play instead.
        const firstTs = options.fromTs > 0 || !times.length ? options.fromTs : times[0];
        firstDay = ListeningVisuals.startOfDay(new Date((firstTs || Date.now() / 1000) * 1000));
        lastDay = ListeningVisuals.startOfDay(new Date((options.toTs - 1) * 1000));
        monthly = Math.round((lastDay - firstDay) / 864e5) > MONTHLY_AFTER_DAYS;
        buckets = [];
        for (let b = bucketStart(firstDay); b <= lastDay; b = nextBucket(b)) buckets.push(b);

        buildRiver();
        remember();
        updateSummary();
        setUpControls();
        render();

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

        const canvas = document.getElementById("river-canvas");
        canvas.addEventListener("pointermove", onPointer);
        canvas.addEventListener("pointerdown", onPointer);
        canvas.addEventListener("pointerleave", hideHover);
        document.getElementById("river-download").addEventListener("click", download);
    }

    return { init };
})();
