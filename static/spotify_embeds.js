// Shared Spotify track previews used by the Duplicates, Playlist Diff, and
// Search pages. Embeds are created through Spotify's iFrame API so that
// starting one preview pauses any other preview playing on the page.
//
// Server-rendered placeholders (`<div data-spotify-embed data-uri="...">`)
// are mounted on load; pages that build results in JS call
// `SpotifyEmbeds.mount(el)` themselves.

window.SpotifyEmbeds = (function () {
    let iframeApi = null;
    const pending = [];
    // Each entry: { el, controller, playing }.
    const embeds = [];

    window.onSpotifyIframeApiReady = (api) => {
        iframeApi = api;
        pending.splice(0).forEach(create);
    };
    const script = document.createElement("script");
    script.src = "https://open.spotify.com/embed/iframe-api/v1";
    script.async = true;
    document.head.appendChild(script);

    // Drop embeds that have been removed from the page (e.g. replaced
    // search results), so they stop listening for messages.
    function prune() {
        for (let i = embeds.length - 1; i >= 0; i--) {
            if (!embeds[i].el.isConnected) {
                if (embeds[i].controller) embeds[i].controller.destroy();
                embeds.splice(i, 1);
            }
        }
    }

    function create(el) {
        // The API replaces its target with the iframe, so give it a child
        // and keep `el` (and its styling) as the wrapper.
        const target = document.createElement("div");
        el.appendChild(target);
        const entry = { el, controller: null, playing: false };
        embeds.push(entry);
        iframeApi.createController(
            target,
            { uri: el.dataset.uri, width: "100%", height: 80, theme: "dark" },
            (controller) => {
                entry.controller = controller;
                controller.addListener("playback_update", (e) => {
                    const playing = !e.data.isPaused;
                    if (playing && !entry.playing) {
                        embeds.forEach((other) => {
                            if (other !== entry && other.playing) {
                                other.controller.pause();
                            }
                        });
                    }
                    entry.playing = playing;
                });
            },
        );
    }

    function mount(el) {
        prune();
        if (iframeApi) create(el);
        else pending.push(el);
    }

    document.addEventListener("DOMContentLoaded", () => {
        document.querySelectorAll("[data-spotify-embed]").forEach(mount);
    });

    return { mount };
})();
