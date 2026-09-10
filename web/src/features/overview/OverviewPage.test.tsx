import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import i18n from "../../i18n";
import { OverviewPage } from "./OverviewPage";

test("renders translated system facts", async () => {
  render(<OverviewPage systemStatus={{ state: "ready", value: { version: "0.1.0", status: "healthy", data_dir: "/tmp/data", workers: [{ engine_id: "fake", status: "ready", message: "" }] } }} />);
  expect(screen.getByRole("heading", { name: "System overview" })).toBeVisible();
  expect(screen.getByText("Ready")).toBeVisible();
});

test("renders Vietnamese overview copy", async () => {
  await i18n.changeLanguage("vi-VN");
  render(<OverviewPage systemStatus={{ state: "ready", value: { version: "0.1.0", status: "healthy", data_dir: "/tmp/data", workers: [] } }} />);
  expect(screen.getByRole("heading", { name: "Tổng quan hệ thống" })).toBeVisible();
  expect(screen.getByText(/Hiện chưa có Worker/)).toBeVisible();
  await i18n.changeLanguage("en-US");
});
