// Listening Shape: every scrobble as a glowing stroke coloured from its
// album art, laid out in one of several shapes. Every shape is drawn from
// the same plays, so switching shape is just a redraw. A stroke runs from
// when a track started until the next one did - roughly how long it played
// - so a listening session reads as one strand changing colour track by
// track. Drawn on a canvas so thousands of strokes stay fast and the PNG
// export is just a bigger redraw.
const ListeningShape = (() => {
    const SECONDS_PER_DAY = 24 * 60 * 60;
    const SECONDS_PER_WEEK = 7 * SECONDS_PER_DAY;
    const EXPORT_WIDTH = 2400;
    const SESSION_GAP_SECONDS = 10 * 60;
    const LONE_PLAY_SECONDS = 3.5 * 60;
    // Shortest stroke, in line widths (before its round caps), so even the
    // shortest play is a pill about twice as wide as it is tall.
    const MIN_STROKE_LENGTH = 1;
    const HOUR_GUIDES = [0, 6, 12, 18];

    // period: how much time one ring or row covers. layout: "radial"
    // (rings, newest outermost, starting at the top and going clockwise) or
    // "rows" (oldest at the top, left to right). stacked: every period on
    // the same ring, instead of one ring each.
    const SHAPES = {
        clock: {
            label: "Clock",
            period: "day",
            layout: "radial",
            description: "One ring per day, newest on the outside, going round from midnight at the top.",
        },
        weekClock: {
            label: "Week clock",
            period: "week",
            layout: "radial",
            description: "One ring per week, newest on the outside, going round from Monday to Sunday.",
        },
        dayRows: {
            label: "Day rows",
            period: "day",
            layout: "rows",
            description: "One row per day, midnight to midnight, oldest at the top.",
        },
        weekRows: {
            label: "Week rows",
            period: "week",
            layout: "rows",
            description: "One row per week, Monday to Sunday, oldest at the top.",
        },
        exposure: {
            label: "Long exposure",
            period: "day",
            layout: "radial",
            stacked: true,
            description:
                "Every day stacked onto one 24-hour ring, like a long-exposure photo - the times of day you listen most glow brightest.",
        },
    };
    const DEFAULT_SHAPE = "clock";

    let plays = [];
    let firstDay;
    let lastDay;
    let dayCount = 1;
    let firstWeekday = 0; // of firstDay, Monday = 0
    let shapeKey = DEFAULT_SHAPE;
    let model;
    let settings = {};
    let shownRange = {};
    let theme = {};
    const adjustedColours = new Map();

    function cssVar(name) {
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    }

    function hexToRgb(hex) {
        const n = parseInt(hex.slice(1), 16);
        return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    }

    function luminance([r, g, b]) {
        return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
    }

    function rgbToHsl([r, g, b]) {
        [r, g, b] = [r / 255, g / 255, b / 255];
        const max = Math.max(r, g, b);
        const min = Math.min(r, g, b);
        const l = (max + min) / 2;
        if (max === min) return [0, 0, l];
        const d = max - min;
        const s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
        let h;
        if (max === r) h = (g - b) / d + (g < b ? 6 : 0);
        else if (max === g) h = (b - r) / d + 2;
        else h = (r - g) / d + 4;
        return [h / 6, s, l];
    }

    function hslToRgb([h, s, l]) {
        if (s === 0) return [l, l, l].map((c) => Math.round(c * 255));
        const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
        const p = 2 * l - q;
        const channel = (t) => {
            if (t < 0) t += 1;
            if (t > 1) t -= 1;
            if (t < 1 / 6) return p + (q - p) * 6 * t;
            if (t < 1 / 2) return q;
            if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
            return p;
        };
        return [channel(h + 1 / 3), channel(h), channel(h - 1 / 3)].map((c) => Math.round(c * 255));
    }

    // Album art's dominant colours are often muted, so coloured ones get a
    // saturation lift, and every colour's lightness is kept in a band that
    // reads well on the background (no black strokes on the dark theme or
    // white ones on the light theme). Greys stay grey.
    function visibleColour(hex) {
        if (!hex) return theme.neutral;
        if (adjustedColours.has(hex)) return adjustedColours.get(hex);
        let [h, s, l] = rgbToHsl(hexToRgb(hex));
        if (s >= 0.12) s = Math.min(1, s * 1.4 + 0.08);
        l = theme.dark ? Math.min(Math.max(l, 0.38), 0.78) : Math.min(Math.max(l, 0.28), 0.62);
        const [r, g, b] = hslToRgb([h, s, l]);
        const result = `rgb(${r}, ${g}, ${b})`;
        adjustedColours.set(hex, result);
        return result;
    }

    function readTheme() {
        const bg = getComputedStyle(document.body).backgroundColor;
        const rgb = (bg.match(/\d+/g) || [255, 255, 255]).slice(0, 3).map(Number);
        theme = {
            bg,
            dark: luminance(rgb) < 0.5,
            neutral: cssVar("--muted") || "#888",
            guide: cssVar("--border") || "#ccc",
            label: cssVar("--muted") || "#888",
            text: getComputedStyle(document.body).color,
        };
        adjustedColours.clear();
    }

    const clamp = (value, min, max) => Math.min(Math.max(value, min), max);

    // Short weekday names in the browser's language, Monday first.
    function weekdayNames() {
        const monday = new Date(2024, 0, 1);
        return Array.from({ length: 7 }, (_, i) =>
            ListeningVisuals.addDays(monday, i).toLocaleDateString(undefined, { weekday: "short" })
        );
    }

    // Where each play sits in the current shape: its ring/row, and how far
    // through that ring/row's period it starts and ends (in seconds).
    // Strands stop at the end of their period - midnight for day shapes,
    // Sunday midnight for week shapes.
    function buildModel() {
        const config = SHAPES[shapeKey];
        const weekly = config.period === "week";
        const span = weekly ? SECONDS_PER_WEEK : SECONDS_PER_DAY;
        const rowOf = (day) => {
            if (config.stacked) return 0;
            return weekly ? Math.floor((day + firstWeekday) / 7) : day;
        };
        const rowCount = rowOf(dayCount - 1) + 1;
        const rows = Array.from({ length: rowCount }, () => []);
        plays.forEach((play) => {
            play.row = rowOf(play.day);
            play.offset = weekly ? ((play.day + firstWeekday) % 7) * SECONDS_PER_DAY + play.seconds : play.seconds;
            play.end = Math.min(play.offset + play.length, span);
            rows[play.row].push(play);
        });
        // The date each ring/row starts on, for labels.
        const rowStart = (row) =>
            ListeningVisuals.addDays(firstDay, weekly ? row * 7 - firstWeekday : row);
        return { config, weekly, span, rowCount, rows, rowStart };
    }

    function geometry(width) {
        const { config, rowCount } = model;
        if (config.layout === "radial") {
            const pad = width * 0.075;
            const outer = width / 2 - pad;
            // Stacked plays share one narrow band, so they pile up.
            const inner = outer * (config.stacked ? 0.68 : 0.14);
            const ringWidth = (outer - inner) / rowCount;
            // The day clock's strokes are a little wider than a ring, so
            // neighbouring days touch and blend into bands; the week clock's
            // are slimmer, since a week ring is far more crowded.
            let lineWidth;
            if (config.stacked) lineWidth = width * 0.012;
            else if (config.period === "week") lineWidth = clamp(ringWidth * 0.75, width * 0.003, width * 0.014);
            else lineWidth = clamp(ringWidth * 1.1, width * 0.004, width * 0.02);
            return { radial: true, width, height: width, centre: width / 2, pad, outer, inner, ringWidth, lineWidth };
        }
        const height = width * clamp(rowCount * 0.035 + 0.15, 0.45, 1.25);
        const left = width * 0.12;
        const top = width * 0.07;
        const plotWidth = width - left - width * 0.03;
        const rowHeight = (height - top - width * 0.02) / rowCount;
        // Thinner than the rings: a row spans a whole day or week, so a
        // track is only a pixel or two long and a fat stroke is all end-cap.
        const lineWidth = clamp(rowHeight * 0.4, width * 0.0025, width * 0.008);
        return { radial: false, width, height, left, top, plotWidth, rowHeight, lineWidth };
    }

    function angleOf(offset) {
        return (offset / model.span) * 2 * Math.PI - Math.PI / 2;
    }

    // Stacked plays are scattered across the ring's band (by a hash of
    // their timestamp, so the picture is the same every time), which turns
    // the overlapping strands into star-trail-like streaks.
    function radiusOf(play, geo) {
        if (model.config.stacked) return geo.inner + play.scatter * (geo.outer - geo.inner);
        return geo.inner + (play.row + 0.5) * geo.ringWidth;
    }

    function rowY(row, geo) {
        return geo.top + (row + 0.5) * geo.rowHeight;
    }

    function pointAt(play, offset, geo) {
        if (geo.radial) {
            const r = radiusOf(play, geo);
            const a = angleOf(offset);
            return [geo.centre + r * Math.cos(a), geo.centre + r * Math.sin(a)];
        }
        return [geo.left + (offset / model.span) * geo.plotWidth, rowY(play.row, geo)];
    }

    // Where a play's stroke starts and ends, in seconds through its period.
    // A track is often only a pixel or two long at this scale, which would
    // leave its round caps meeting in a dot - so every stroke is at least
    // MIN_STROKE_LENGTH line widths long (plus its caps), making the
    // shortest plays pills that point along the ring or row. Kept inside
    // the period, so it never runs off the end of a row.
    function strokeExtent(play, geo) {
        const pixelsPerSecond = geo.radial
            ? (radiusOf(play, geo) * 2 * Math.PI) / model.span
            : geo.plotWidth / model.span;
        const minSeconds = (geo.lineWidth * MIN_STROKE_LENGTH) / pixelsPerSecond;
        if (play.end - play.offset >= minSeconds) return [play.offset, play.end];
        const start = Math.max(0, Math.min(play.offset, model.span - minSeconds));
        return [start, start + minSeconds];
    }

    function traceStrand(ctx, play, geo) {
        const [start, end] = strokeExtent(play, geo);
        ctx.moveTo(...pointAt(play, start, geo));
        if (geo.radial) {
            ctx.arc(geo.centre, geo.centre, radiusOf(play, geo), angleOf(start), angleOf(end));
        } else {
            ctx.lineTo(...pointAt(play, end, geo));
        }
    }

    function guideFont(geo) {
        return `${Math.round(geo.width * (geo.radial ? 0.028 : 0.022))}px sans-serif`;
    }

    // Time-of-day or day-of-week guides, with labels.
    function guideMarks() {
        if (model.weekly) {
            const names = weekdayNames();
            return {
                lines: Array.from({ length: 8 }, (_, i) => i * SECONDS_PER_DAY),
                labels: names.map((name, i) => [name, (i + 0.5) * SECONDS_PER_DAY]),
            };
        }
        const hours = model.config.layout === "rows" ? [...HOUR_GUIDES, 24] : HOUR_GUIDES;
        return {
            lines: hours.map((h) => h * 3600),
            labels: hours.map((h) => [String(h).padStart(2, "0"), h * 3600]),
        };
    }

    function drawGuides(ctx, geo) {
        const marks = guideMarks();
        ctx.strokeStyle = theme.guide;
        ctx.lineWidth = Math.max(1, geo.width / 700);
        ctx.fillStyle = theme.label;
        ctx.font = guideFont(geo);
        ctx.textBaseline = "middle";

        if (geo.radial) {
            ctx.textAlign = "center";
            ctx.beginPath();
            marks.lines.forEach((offset) => {
                const a = angleOf(offset);
                const [cos, sin] = [Math.cos(a), Math.sin(a)];
                ctx.moveTo(geo.centre + geo.inner * cos, geo.centre + geo.inner * sin);
                ctx.lineTo(geo.centre + (geo.outer + geo.pad * 0.2) * cos, geo.centre + (geo.outer + geo.pad * 0.2) * sin);
            });
            ctx.stroke();
            marks.labels.forEach(([text, offset]) => {
                const a = angleOf(offset);
                const r = geo.outer + geo.pad * 0.55;
                ctx.fillText(text, geo.centre + r * Math.cos(a), geo.centre + r * Math.sin(a));
            });
        } else {
            const bottom = geo.top + model.rowCount * geo.rowHeight;
            const xAt = (offset) => geo.left + (offset / model.span) * geo.plotWidth;
            ctx.beginPath();
            marks.lines.forEach((offset) => {
                ctx.moveTo(xAt(offset), geo.top - geo.width * 0.01);
                ctx.lineTo(xAt(offset), bottom);
            });
            ctx.stroke();
            ctx.textAlign = "center";
            marks.labels.forEach(([text, offset]) => ctx.fillText(text, xAt(offset), geo.top * 0.45));

            // Dates down the left, thinned out so they never overlap.
            ctx.textAlign = "right";
            const fontSize = geo.width * 0.022;
            const step = Math.max(1, Math.ceil((fontSize * 1.6) / geo.rowHeight));
            const dateOptions = { day: "numeric", month: "short" };
            for (let row = 0; row < model.rowCount; row += step) {
                const label = model.rowStart(row).toLocaleDateString(undefined, dateOptions);
                ctx.fillText(label, geo.left - geo.width * 0.015, rowY(row, geo));
            }
        }

        if (model.config.stacked) return;
        // A faint line for every ring or row, like the grooves on a record,
        // so the empty parts still have structure.
        ctx.globalAlpha = theme.dark ? 0.35 : 0.5;
        ctx.lineWidth = Math.max(0.5, geo.width / 1800);
        ctx.beginPath();
        for (let row = 0; row < model.rowCount; row++) {
            if (geo.radial) {
                const r = geo.inner + (row + 0.5) * geo.ringWidth;
                ctx.moveTo(geo.centre + r, geo.centre);
                ctx.arc(geo.centre, geo.centre, r, 0, 2 * Math.PI);
            } else {
                ctx.moveTo(geo.left, rowY(row, geo));
                ctx.lineTo(geo.left + geo.plotWidth, rowY(row, geo));
            }
        }
        ctx.stroke();
        ctx.globalAlpha = 1;
    }

    function draw(ctx, geo, pixelRatio = 1) {
        ctx.fillStyle = theme.bg;
        ctx.fillRect(0, 0, geo.width, geo.height);
        drawGuides(ctx, geo);

        // One path per colour - far fewer draw calls than one per play.
        const byColour = new Map();
        plays.forEach((play) => {
            const colour = visibleColour(play.c);
            if (!byColour.has(colour)) byColour.set(colour, []);
            byColour.get(colour).push(play);
        });
        const strokeAll = () =>
            byColour.forEach((group, colour) => {
                ctx.strokeStyle = colour;
                ctx.shadowColor = colour;
                ctx.beginPath();
                group.forEach((play) => traceStrand(ctx, play, geo));
                ctx.stroke();
            });
        ctx.lineCap = "round";
        ctx.lineWidth = geo.lineWidth;

        // On the dark theme glows are additive, so busy stretches, where
        // they overlap, shine brighter than quiet ones. Stacked strands are
        // faint individually - their brightness comes from piling up.
        const stacked = model.config.stacked;
        const pileUp = stacked ? clamp(10 / dayCount, 0.2, 0.9) : 1;
        if (theme.dark) ctx.globalCompositeOperation = "lighter";
        // Rows get a fainter, tighter glow, since their strokes sit close
        // together on straight lines and a big glow smears them.
        const glowAlpha = geo.radial ? (theme.dark ? 0.5 : 0.35) : theme.dark ? 0.3 : 0.2;
        ctx.globalAlpha = glowAlpha * pileUp;
        // shadowBlur ignores the canvas transform, so it's scaled by hand.
        ctx.shadowBlur = geo.lineWidth * (geo.radial ? 2.5 : 0.8) * pixelRatio;
        strokeAll();

        // Then the sharp strokes on top.
        if (!stacked) ctx.globalCompositeOperation = "source-over";
        ctx.shadowBlur = 0;
        ctx.shadowColor = "transparent";
        ctx.globalAlpha = stacked ? pileUp : 0.95;
        strokeAll();
        ctx.globalCompositeOperation = "source-over";
        ctx.globalAlpha = 1;
    }

    function sizeCanvas(canvas, geo) {
        const ratio = window.devicePixelRatio || 1;
        canvas.width = Math.round(geo.width * ratio);
        canvas.height = Math.round(geo.height * ratio);
        const ctx = canvas.getContext("2d");
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
        return [ctx, ratio];
    }

    function render() {
        readTheme();
        const canvas = document.getElementById("shape-canvas");
        const geo = geometry(canvas.parentElement.clientWidth);
        const [ctx, ratio] = sizeCanvas(canvas, geo);
        draw(ctx, geo, ratio);
        sizeCanvas(document.getElementById("shape-overlay"), geo);
        hideTooltip();
    }

    // The play whose strand is nearest a point on the canvas, or null if
    // none is close. Measures along the strand's ring/row (how far the
    // point is past either end) and across it (how far off the line).
    function playAt(x, y, geo) {
        let candidates;
        let pointOffset;
        let across;
        let pixelsPerSecond;
        if (geo.radial) {
            const dx = x - geo.centre;
            const dy = y - geo.centre;
            const distance = Math.hypot(dx, dy);
            pointOffset = ((((Math.atan2(dy, dx) + Math.PI / 2) / (2 * Math.PI)) % 1) + 1) % 1 * model.span;
            const row = Math.floor((distance - geo.inner) / geo.ringWidth);
            candidates = model.config.stacked ? plays : [row - 1, row, row + 1].flatMap((r) => model.rows[r] || []);
            across = (play) => Math.abs(distance - radiusOf(play, geo));
            pixelsPerSecond = (play) => (radiusOf(play, geo) * 2 * Math.PI) / model.span;
        } else {
            pointOffset = ((x - geo.left) / geo.plotWidth) * model.span;
            const row = Math.floor((y - geo.top) / geo.rowHeight);
            candidates = [row - 1, row, row + 1].flatMap((r) => model.rows[r] || []);
            across = (play) => Math.abs(y - rowY(play.row, geo));
            pixelsPerSecond = () => geo.plotWidth / model.span;
        }
        let best = null;
        let bestDistance = Infinity;
        candidates.forEach((play) => {
            const [start, end] = strokeExtent(play, geo);
            const past = Math.max(0, start - pointOffset, pointOffset - end);
            const distance = Math.hypot(past * pixelsPerSecond(play), across(play));
            if (distance < bestDistance) {
                best = play;
                bestDistance = distance;
            }
        });
        return bestDistance <= Math.max(geo.lineWidth, 8) ? best : null;
    }

    function hideTooltip() {
        document.getElementById("shape-tooltip").style.display = "none";
        const overlay = document.getElementById("shape-overlay");
        overlay.getContext("2d").clearRect(0, 0, overlay.width, overlay.height);
    }

    function showTooltip(play, geo, x, y) {
        // Outline the whole strand: a wider stroke in the text colour, then
        // the strand's own colour on top.
        const ctx = document.getElementById("shape-overlay").getContext("2d");
        ctx.clearRect(0, 0, geo.width, geo.height);
        ctx.lineCap = "round";
        [
            [theme.text, geo.lineWidth + 4],
            [visibleColour(play.c), geo.lineWidth],
        ].forEach(([colour, width]) => {
            ctx.strokeStyle = colour;
            ctx.lineWidth = width;
            ctx.beginPath();
            traceStrand(ctx, play, geo);
            ctx.stroke();
        });

        const tooltip = document.getElementById("shape-tooltip");
        tooltip.replaceChildren();
        const title = document.createElement("div");
        title.textContent = play.n;
        const artist = document.createElement("div");
        artist.textContent = play.a;
        const when = document.createElement("div");
        when.className = "when";
        when.textContent = play.date.toLocaleString(undefined, {
            weekday: "short",
            day: "numeric",
            month: "short",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit",
        });
        tooltip.append(title, artist, when);
        tooltip.style.display = "block";
        const left = x + 14 + tooltip.offsetWidth > geo.width ? x - 14 - tooltip.offsetWidth : x + 14;
        tooltip.style.left = `${Math.max(0, left)}px`;
        tooltip.style.top = `${Math.max(0, Math.min(y + 14, geo.height - tooltip.offsetHeight))}px`;
    }

    function onPointer(event) {
        const canvas = document.getElementById("shape-canvas");
        const rect = canvas.getBoundingClientRect();
        const geo = geometry(rect.width);
        const x = event.clientX - rect.left;
        const y = event.clientY - rect.top;
        const play = playAt(x, y, geo);
        if (play) showTooltip(play, geo, x, y);
        else hideTooltip();
    }

    function download() {
        const geo = geometry(EXPORT_WIDTH);
        const canvas = document.createElement("canvas");
        canvas.width = geo.width;
        canvas.height = Math.round(geo.height);
        draw(canvas.getContext("2d"), geo);
        const slug = SHAPES[shapeKey].label.toLowerCase().replace(/\s+/g, "-");
        canvas.toBlob((blob) => {
            const link = document.createElement("a");
            link.href = URL.createObjectURL(blob);
            link.download = `listening-shape-${slug}-${shownRange.start}-to-${shownRange.end}.png`;
            link.click();
            setTimeout(() => URL.revokeObjectURL(link.href), 1000);
        }, "image/png");
    }

    function describeRange(first, last) {
        const opts = { day: "numeric", month: "short", year: "numeric" };
        return `${first.toLocaleDateString(undefined, opts)} – ${last.toLocaleDateString(undefined, opts)}`;
    }

    function remember() {
        ListeningVisuals.remember("shape", {
            range: settings.range,
            start: shownRange.start,
            end: shownRange.end,
            shape: shapeKey,
        });
    }

    function selectShape(key) {
        shapeKey = key;
        model = buildModel();
        document.querySelectorAll("#shape-picker button").forEach((button) => {
            button.setAttribute("aria-pressed", String(button.dataset.shape === key));
        });
        document.getElementById("shape-description").textContent = SHAPES[key].description;
        remember();
        render();
    }

    function setUpShapePicker() {
        const picker = document.getElementById("shape-picker");
        Object.entries(SHAPES).forEach(([key, config]) => {
            const button = document.createElement("button");
            button.type = "button";
            button.dataset.shape = key;
            button.textContent = config.label;
            button.addEventListener("click", () => selectShape(key));
            picker.appendChild(button);
        });
    }

    function setUpRangeControls(maxDays) {
        const form = document.getElementById("shape-controls");
        const select = document.getElementById("shape-range");
        const start = document.getElementById("shape-start");
        const end = document.getElementById("shape-end");
        const error = document.getElementById("shape-range-error");
        const today = ListeningVisuals.formatDate(new Date());

        select.value = settings.range === "custom" ? "custom" : String(settings.range);
        if (!select.value) select.value = "30";
        start.value = shownRange.start;
        end.value = shownRange.end;
        start.max = today;
        end.max = today;
        form.classList.toggle("custom", select.value === "custom");

        select.addEventListener("change", () => {
            if (select.value === "custom") {
                form.classList.add("custom");
                start.focus();
            } else {
                ListeningVisuals.open("shape", { range: select.value });
            }
        });

        form.addEventListener("submit", (event) => {
            event.preventDefault();
            const first = ListeningVisuals.parseDate(start.value);
            const last = ListeningVisuals.parseDate(end.value);
            let message = "";
            if (!first || !last) message = "Pick a start and end date.";
            else if (first > last) message = "The start date needs to be before the end date.";
            else if (Math.round((last - first) / 864e5) + 1 > maxDays) message = `Pick a range of up to ${maxDays} days.`;
            error.textContent = message;
            error.style.display = message ? "block" : "none";
            if (!message) ListeningVisuals.open("shape", { range: "custom", start: start.value, end: end.value });
        });
    }

    function init(options) {
        settings = options.settings;
        firstDay = ListeningVisuals.startOfDay(new Date(options.fromTs * 1000));
        lastDay = ListeningVisuals.startOfDay(new Date((options.toTs - 1) * 1000));
        firstWeekday = (firstDay.getDay() + 6) % 7;
        const dayIndex = (date) => Math.round((ListeningVisuals.startOfDay(date) - firstDay) / 864e5);
        dayCount = dayIndex(lastDay) + 1;
        shownRange = {
            start: ListeningVisuals.formatDate(firstDay),
            end: ListeningVisuals.formatDate(lastDay),
        };

        plays = options.plays
            .map((play) => {
                const date = new Date(play.t * 1000);
                const seconds = date.getHours() * 3600 + date.getMinutes() * 60 + date.getSeconds();
                // A stable pseudo-random 0-1 from the timestamp, for where a
                // play sits in the long exposure's band.
                const scatter = Math.abs(Math.sin(play.t * 12.9898) * 43758.5453) % 1;
                return { ...play, date, seconds, day: dayIndex(date), scatter };
            })
            .filter((play) => play.day >= 0 && play.day < dayCount);
        // How long each play lasts as a strand: until the next play, if
        // that came within the session gap, else a typical track length.
        plays.forEach((play, i) => {
            const next = plays[i + 1];
            const gap = next ? next.t - play.t : Infinity;
            play.length = gap <= SESSION_GAP_SECONDS ? gap : LONE_PLAY_SECONDS;
        });

        const noArt = plays.filter((play) => !play.c).length;
        const count = plays.length === 1 ? "1 play" : `${plays.length.toLocaleString()} plays`;
        document.getElementById("shape-summary").textContent = plays.length
            ? `${count} over ${dayCount} days, ${describeRange(firstDay, lastDay)}` +
              (noArt ? ` · ${noArt.toLocaleString()} without album art, shown in grey` : "")
            : `No plays between ${describeRange(firstDay, lastDay)}.`;

        setUpRangeControls(options.maxDays);
        setUpShapePicker();
        const saved = ListeningVisuals.savedSettings("shape").shape;
        selectShape(SHAPES[saved] ? saved : DEFAULT_SHAPE);

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

        const canvas = document.getElementById("shape-canvas");
        canvas.addEventListener("pointermove", onPointer);
        canvas.addEventListener("pointerdown", onPointer);
        canvas.addEventListener("pointerleave", hideTooltip);
        document.getElementById("shape-download").addEventListener("click", download);
    }

    return { init };
})();
