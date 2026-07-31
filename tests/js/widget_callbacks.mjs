// Behaviour check for the JS this package generates into the payment page.
//
// The widget callbacks are built as Python string literals in provider.py, so
// nothing in a normal Python test run ever executes them: for years
// cardSuccess called jQuery's $.post, and hosts that do not load jQuery only
// found out in production, at the moment the buyer confirmed their card.
//
// Usage: node tests/js/widget_callbacks.mjs <file-with-rendered-form-html>
// Exits non-zero and prints the failures; silent on success.

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";

const html = readFileSync(process.argv[2], "utf8");

// Inline scripts only - the <script src=...> tags are PayU's own bundle.
const inlineScripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
    .map((m) => m[1])
    .join("\n");

const failures = [];
const check = (label, actual, expected) => {
    const ok = JSON.stringify(actual) === JSON.stringify(expected);
    if (!ok) failures.push(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
};

function makeSandbox({ fetchImpl }) {
    const logged = [];
    const sandbox = {
        window: { location: { href: "__UNCHANGED__" } },
        fetch: fetchImpl,
        URLSearchParams,
        Object,
        JSON,
        console: { log: () => {}, error: (...a) => logged.push(String(a[0])) },
        __logged: logged,
    };
    sandbox.globalThis = sandbox;
    createContext(sandbox);
    // No jQuery in this context on purpose: a callback that reaches for $
    // must fail here rather than in a buyer's browser.
    runInContext(inlineScripts, sandbox);
    return sandbox;
}

const settle = () => new Promise((r) => setTimeout(r, 10));

// 1. Happy path: the tokenized card is posted and the browser follows the
//    URL the server answers with.
{
    let call = null;
    const sandbox = makeSandbox({
        fetchImpl: (url, init) => {
            call = { url, init };
            return Promise.resolve({ text: () => Promise.resolve("https://host/after-payment") });
        },
    });
    await sandbox.cardSuccess({ value: "TOK-123", type: "MULTI", empty: null, absent: undefined });
    await settle();

    if (!call) {
        failures.push("happy path: no request was made at all");
    } else {
        check("method", call.init.method, "POST");
        check(
            "content-type",
            call.init.headers["Content-Type"],
            "application/x-www-form-urlencoded",
        );
        // process_data() reads request.POST['value']; null/undefined entries
        // must not be sent as the strings "null"/"undefined".
        check("body", new URLSearchParams(call.init.body).get("value"), "TOK-123");
        check("body drops empty values", call.init.body.includes("empty"), false);
        check("redirect", sandbox.window.location.href, "https://host/after-payment");
    }
}

// 2. A failed post must not navigate, must be logged, and must not escape as
//    an unhandled rejection - the buyer has already confirmed the card.
{
    let unhandled = false;
    const onUnhandled = () => {
        unhandled = true;
    };
    process.on("unhandledRejection", onUnhandled);
    const sandbox = makeSandbox({ fetchImpl: () => Promise.reject(new TypeError("Failed to fetch")) });
    await sandbox.cardSuccess({ value: "TOK-123" });
    await settle();
    process.off("unhandledRejection", onUnhandled);

    check("network failure does not navigate", sandbox.window.location.href, "__UNCHANGED__");
    check("network failure is logged", sandbox.__logged.length > 0, true);
    check("network failure is handled", unhandled, false);
}

// 3. A payload-less call must not throw (the widget has been seen to invoke
//    the callback with nothing on some flows).
{
    const sandbox = makeSandbox({
        fetchImpl: () => Promise.resolve({ text: () => Promise.resolve("https://host/x") }),
    });
    try {
        await sandbox.cardSuccess(undefined);
        await settle();
    } catch (err) {
        failures.push(`undefined payload threw: ${err.message}`);
    }
}

// 4. cvvSuccess just redirects to the success URL.
{
    const sandbox = makeSandbox({ fetchImpl: () => Promise.reject(new Error("must not be called")) });
    sandbox.cvvSuccess({});
    await settle();
    check("cvv redirects", sandbox.window.location.href !== "__UNCHANGED__", true);
}

if (failures.length) {
    console.error("Widget callback JS failures:\n  " + failures.join("\n  "));
    process.exit(1);
}
