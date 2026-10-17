"""English interface strings that are not their own key.

English is the language the interface is written in, so most strings need no table: the
code's literal is what the user reads (see konus.i18n). A few strings are built from a
namespaced key instead, because the plain word already means something else elsewhere —
the three headings of the Settings rail are the case that made this file: "System" is a
key of its own (the theme option, "Системная"), so the rail's grouping headings are
looked up as "group:System" and would otherwise be shown as that key, colon and all.

Keys are the same namespaced literals the code looks up.
"""

TABLE = {
    "group:App": "App",
    "group:Media": "Media",
    "group:System": "System",
}
