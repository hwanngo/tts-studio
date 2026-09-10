import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test } from "vitest";
import i18n from "../../i18n";
import { Combobox } from "./combobox";

test("localizes the combobox toggle labels", async () => {
  await i18n.changeLanguage("vi-VN");
  const user = userEvent.setup();
  render(<Combobox label="Model" options={[{ value: "one", label: "One" }]} value="" onValueChange={() => undefined} />);
  const toggle = screen.getByRole("button", { name: "Mở tùy chọn" });
  await user.click(toggle);
  expect(screen.getByRole("button", { name: "Đóng tùy chọn" })).toBeVisible();
  await i18n.changeLanguage("en-US");
});
