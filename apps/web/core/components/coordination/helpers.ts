/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

function messages(value: unknown): string[] {
  if (typeof value === "string") return [value];
  if (Array.isArray(value)) return value.flatMap(messages);
  if (typeof value === "object" && value !== null) return Object.values(value).flatMap(messages);
  return [];
}

export function coordinationError(error: unknown): string {
  if (typeof error !== "object" || error === null) return "The request failed. Try again.";
  const response = "response" in error ? error.response : undefined;
  const data = typeof response === "object" && response !== null && "data" in response ? response.data : undefined;
  return messages(data).join(" ") || "The request failed. Try again.";
}

export function durationSeconds(value: string | null): number | "" {
  if (!value) return "";
  const parts = value.trim().split(/\s+/);
  const time = parts.at(-1) ?? "";
  const seconds = time.split(":").reduce((sum, part) => sum * 60 + Number(part), 0);
  const total = seconds + (parts.length > 1 ? Number(parts[0]) * 86400 : 0);
  return Number.isFinite(total) ? total : "";
}
