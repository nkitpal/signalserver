import { expect, test, type Page } from "@playwright/test";

/** Answer the next browser prompt/confirm dialogs in order. */
function answerDialogs(page: Page, answers: (string | boolean)[]) {
  const queue = [...answers];
  const handler = async (d: import("@playwright/test").Dialog) => {
    const a = queue.shift();
    if (a === false) await d.dismiss();
    else await d.accept(typeof a === "string" ? a : undefined);
    if (!queue.length) page.off("dialog", handler);
  };
  page.on("dialog", handler);
}

const KML = `<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>E2E survey</name>
<Placemark><name>Survey A</name><Point><coordinates>9.08,47.12,0</coordinates></Point></Placemark>
<Placemark><name>Route</name><LineString><coordinates>9.03,47.08 9.08,47.12</coordinates></LineString></Placemark>
</Document></kml>`;

test("project → folder → station → plot → run, compare, measure, import KML", async ({ page, request }) => {
  const suffix = Date.now().toString(36);
  const project = `E2E ${suffix}`;
  await page.goto("/");
  await page.waitForFunction(() => document.querySelector("#globe canvas") !== null);

  // Project and folder (names come from prompts).
  answerDialogs(page, [project]);
  await page.getByRole("button", { name: "+ Project" }).click();
  await expect(page.locator(".project-select")).toHaveValue(/\d+/);
  await expect(page.locator(".project-select option:checked")).toHaveText(project);
  answerDialogs(page, ["Sites"]);
  await page.getByRole("button", { name: "+ Folder" }).click();
  await expect(page.locator(".tree .row.selected", { hasText: "Sites" })).toBeVisible();

  // Station: pick on the globe, then refine the coordinates.
  await page.getByRole("button", { name: "+ Station" }).click();
  await page.getByLabel("Name").fill("Hill");
  await page.getByRole("button", { name: /Pick on globe/ }).click();
  const box = (await page.locator("#globe canvas").boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.locator('input[name="latitude"]')).not.toHaveValue("");
  await page.locator('input[name="latitude"]').fill("47.10");
  await page.locator('input[name="longitude"]').fill("9.05");
  await page.locator('input[name="height_m"]').fill("25");
  await page.getByRole("button", { name: "Create station" }).click();
  await expect(page.locator(".tree .row", { hasText: "Hill" })).toBeVisible();

  // Plot in the same folder, run it.
  await page.getByRole("button", { name: "+ Plot", exact: true }).click();
  await page.getByLabel("Name").fill("Hill-UHF");
  await page.locator('input[name="radius_km"]').fill("8");
  await page.locator('select[name="resolution"]').selectOption("600");
  answerDialogs(page, [true]); // "Run it now?"
  await page.getByRole("button", { name: "Create plot" }).click();
  const row = page.locator(".tree .row", { hasText: "Hill-UHF" });
  await expect(row.locator(".badge")).toHaveText(/ready/i, { timeout: 150_000 });
  await row.click();
  const kmz = await page.locator(".details").getByRole("link", { name: "KMZ" }).getAttribute("href");
  const res = await request.get(kmz!);
  expect((await res.body()).subarray(0, 2).toString()).toBe("PK");
  await expect(page.locator("#legend")).toBeVisible();

  // Compare: best server legend lists the plot.
  await page.getByRole("tab", { name: "Analyze" }).click();
  await page.getByRole("radio", { name: "Best server" }).click();
  await expect(page.locator("#legend")).toContainText("Hill-UHF");
  await page.getByRole("radio", { name: "Individual layers" }).click();

  // Measure a distance.
  await page.locator('[data-tool="distance"]').click();
  await page.mouse.click(box.x + box.width * 0.45, box.y + box.height * 0.5);
  await page.mouse.click(box.x + box.width * 0.55, box.y + box.height * 0.5);
  await page.mouse.dblclick(box.x + box.width * 0.55, box.y + box.height * 0.5);
  await expect(page.locator("#measure")).toContainText(/\d+(\.\d+)? (m|km)/);
  await page.locator('[data-tool="distance"]').click();

  // Import a KML into the folder and create a station from its point.
  await page.getByRole("tab", { name: "Explorer" }).click();
  await page.locator(".tree .row", { hasText: "Sites" }).click();
  await page.getByRole("button", { name: "Import KML" }).click();
  await page.locator('input[name="file"]').setInputFiles({ name: "survey.kml", mimeType: "application/xml", buffer: Buffer.from(KML) });
  await page.getByLabel(/create stations/i).check();
  await page.getByRole("button", { name: "Import", exact: true }).click();
  await expect(page.locator(".tree .row", { hasText: "E2E survey" })).toBeVisible();
  await expect(page.locator(".tree .row", { hasText: "Survey A" })).toBeVisible();

  // Antenna library: generate a sector pattern (preview shows polar charts), then a plot
  // that derives its ERP from transmitter power and the antenna's gain.
  await page.getByRole("tab", { name: "Data" }).click();
  await page.getByRole("button", { name: "Generate" }).click();
  await page.locator('input[name="name"]').fill(`Sector ${suffix}`);
  await page.locator('input[name="electrical_tilt"]').fill("4");
  await expect(page.locator("dialog svg.polar")).toHaveCount(2);
  await expect(page.locator("dialog")).toContainText("Downtilt");
  await page.getByRole("button", { name: "Save antenna" }).click();
  await expect(page.locator(".antenna-row", { hasText: `Sector ${suffix}` })).toBeVisible();
  await page.getByRole("tab", { name: "Explorer" }).click();
  await page.getByRole("button", { name: "+ Plot", exact: true }).click();
  await page.getByLabel("Name").fill("Hill-sector");
  const antennaValue = await page.locator('select[name="antenna_id"] option', { hasText: `Sector ${suffix}` }).getAttribute("value");
  await page.locator('select[name="antenna_id"]').selectOption(antennaValue!);
  await page.locator('select[name="power_mode"]').selectOption("tx");
  await page.locator('input[name="tx_power_w"]').fill("10");
  await expect(page.locator(".erp-out")).toContainText("ERP");
  answerDialogs(page, [false]); // don't run it
  await page.getByRole("button", { name: "Create plot" }).click();
  await expect(page.locator(".tree .row", { hasText: "Hill-sector" })).toBeVisible();

  // Clean up: delete the project.
  answerDialogs(page, [true]);
  await page.locator(".project-bar .icon-btn").click();
  await page.getByRole("menuitem", { name: "Delete project" }).click();
  await expect(page.locator(".project-select option", { hasText: project })).toHaveCount(0, { timeout: 20_000 });
});
