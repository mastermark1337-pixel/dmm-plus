"use strict";
const form = document.getElementById("setup-form");
const keyField = document.getElementById("api-key");
const status = document.getElementById("status");
const submit = document.getElementById("create-link");
const manifest = document.getElementById("manifest-link");
document.getElementById("toggle-key").addEventListener("click", function () {
  const show = keyField.type === "password";
  keyField.type = show ? "text" : "password";
  this.textContent = show ? "Hide" : "Show";
  this.setAttribute("aria-label", show ? "Hide API key" : "Show API key");
});
form.addEventListener("submit", async function (event) {
  event.preventDefault();
  document.getElementById("result").hidden = true;
  manifest.value = "";
  submit.disabled = true;
  status.className = "";
  status.textContent = "Checking your TorBox account…";
  try {
    const response = await fetch("/api/configure", {
      method: "POST", headers: {"Content-Type": "application/json"}, cache: "no-store",
      body: JSON.stringify({apiKey: keyField.value.trim(), preferences: {
        preferEnglish: document.getElementById("prefer-english").checked,
        hideCam: document.getElementById("hide-cam").checked,
        maxStreams: Number(document.getElementById("max-streams").value),
        maxFileSizeGB: Number(document.getElementById("max-size").value)
      }})
    });
    const result = await response.json();
    if (!response.ok || !result.manifestUrl) throw new Error(result.error || "Could not create the link. Please try again.");
    manifest.value = result.manifestUrl;
    keyField.value = "";
    keyField.type = "password";
    document.getElementById("toggle-key").textContent = "Show";
    status.className = "success";
    status.textContent = "TorBox account verified.";
    document.getElementById("result").hidden = false;
    document.getElementById("result").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) {
    status.className = "error";
    status.textContent = error.message || "Connection failed. Please try again.";
  } finally {
    submit.disabled = false;
  }
});
async function copy(text, button, fallback) {
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = "Copied";
  } catch (_) {
    if (fallback) { fallback.focus(); fallback.select(); }
    button.textContent = fallback ? "Select the link above and copy it" : text;
  }
}
document.getElementById("copy-link").addEventListener("click", function () { copy(manifest.value, this, manifest); });
document.getElementById("copy-setup").addEventListener("click", function () { copy(location.origin + "/configure", this); });
