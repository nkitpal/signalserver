import { expect, test } from "@playwright/test";

test("create a station on the globe, create a plot and run it", async ({ page, request }) => {
  const suffix = Date.now().toString(36);
  const stationName = `E2E Hill ${suffix}`;
  const plotName = `E2E-plot-${suffix}`;

  await page.goto("/");
  await expect(page.getByRole("tab", { name: "Plots" })).toBeVisible();
  await page.waitForFunction(() => document.querySelector("#globe canvas") !== null);

  // Station: fly near the Alps, pick the location on the globe, then refine coordinates.
  await page.getByRole("tab", { name: "Stations" }).click();
  await page.getByRole("button", { name: "+ New station" }).click();
  await page.getByLabel("Name").fill(stationName);
  await page.getByRole("button", { name: /Pick on globe/ }).click();
  const canvas = page.locator("#globe canvas");
  const box = (await canvas.boundingBox())!;
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.locator('input[name="latitude"]')).not.toHaveValue("");
  await page.locator('input[name="latitude"]').fill("47.10");
  await page.locator('input[name="longitude"]').fill("9.05");
  await page.locator('input[name="height_m"]').fill("25");
  await page.getByRole("button", { name: "Create station" }).click();
  await expect(page.locator(".card h3", { hasText: stationName })).toBeVisible();

  // Plot
  await page.getByRole("tab", { name: "Plots" }).click();
  await page.getByRole("button", { name: "+ New plot" }).click();
  await page.getByLabel("Name").fill(plotName);
  await page.locator('select[name="tx_station_id"]').selectOption({ label: stationName });
  await page.locator('input[name="radius_km"]').fill("8");
  await page.locator('select[name="resolution"]').selectOption("600");
  page.once("dialog", (d) => d.accept()); // "Run it now?"
  await page.getByRole("button", { name: "Create plot" }).click();

  const card = page.locator("article.card", { hasText: plotName });
  await expect(card.locator(".badge")).toHaveText(/ready/i, { timeout: 150_000 });
  await expect(card.getByRole("link", { name: "Google Earth KMZ" })).toBeVisible();
  await expect(page.locator("#legend")).toBeVisible();

  const kmz = await card.getByRole("link", { name: "Google Earth KMZ" }).getAttribute("href");
  const res = await request.get(kmz!);
  expect(res.ok()).toBeTruthy();
  expect((await res.body()).subarray(0, 2).toString()).toBe("PK");

  // Clean up through the UI.
  page.once("dialog", (d) => d.accept());
  await card.getByRole("button", { name: "Delete" }).click();
  await expect(card).toHaveCount(0);
  await page.getByRole("tab", { name: "Stations" }).click();
  page.once("dialog", (d) => d.accept());
  await page.locator("article.card", { hasText: stationName }).getByRole("button", { name: "Delete" }).click();
  await expect(page.locator(".card h3", { hasText: stationName })).toHaveCount(0);
});
