"use strict";
(() => {
  const element = (id) => document.getElementById(id);
  let adminKey = "";
  let profiles = [];
  let busy = false;
  function setBusy(value) {
    busy = value;
    element("unlock-button").disabled = value;
    element("generate").disabled = value;
  }
  function lock() {
    adminKey = "";
    profiles = [];
    element("key").value = "";
    element("customer").replaceChildren();
    for (const id of ["profile", "code", "expiry", "permanent"]) element(id).textContent = "";
    element("directory").hidden = true;
    element("unlock").hidden = false;
  }
  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Authorization": `Bearer ${adminKey}`, ...options.headers },
      cache: "no-store",
      credentials: "omit",
    });
    const value = await response.json();
    if (!response.ok) {
      if (response.status === 401) lock();
      throw new Error(value.error || `Request failed: HTTP ${response.status}`);
    }
    return value;
  }
  function showProfile() {
    const profile = profiles.find((row) => row.candidate_id === element("customer").value);
    element("profile").textContent = profile
      ? `${profile.full_name}\nFictional credit tier: ${profile.credit_tier}\nIndicative maximum: $${profile.indicative_max_loan_usd.toLocaleString()}`
      : "";
    element("code").textContent = "";
    element("expiry").textContent = "";
  }
  element("unlock-button").addEventListener("click", async () => {
    if (busy) return;
    setBusy(true);
    element("status").textContent = "";
    adminKey = element("key").value.trim();
    element("key").value = "";
    try {
      const value = await request("/api/otp/candidates");
      profiles = value.candidates;
      element("customer").replaceChildren(...profiles.map((profile) => {
        const option = document.createElement("option");
        option.value = profile.candidate_id;
        option.textContent = profile.full_name;
        return option;
      }));
      element("permanent").textContent =
        `Permanent fictional demo: ${value.permanent_demo.full_name} - ${value.permanent_demo.otp}`;
      element("unlock").hidden = true;
      element("directory").hidden = false;
      showProfile();
    } catch (error) {
      adminKey = "";
      element("status").textContent = `Could not unlock: ${error.message}`;
    } finally {
      setBusy(false);
    }
  });
  element("generate").addEventListener("click", async () => {
    if (busy) return;
    setBusy(true);
    element("code").textContent = "";
    element("expiry").textContent = "";
    element("status").textContent = "";
    try {
      const value = await request("/api/otp", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ candidate_id: element("customer").value }),
      });
      element("code").textContent = value.otp;
      element("expiry").textContent =
        `Single use. Expires ${new Date(value.expires_at).toLocaleString()}. Replaces this customer's previous code.`;
    } catch (error) {
      element("status").textContent = `Could not generate OTP: ${error.message}`;
    } finally {
      setBusy(false);
    }
  });
  element("customer").addEventListener("change", showProfile);
  element("lock").addEventListener("click", lock);
})();
