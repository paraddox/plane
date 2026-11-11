/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { AxiosInstance, InternalAxiosRequestConfig } from "axios";
import axios, { AxiosHeaders } from "axios";
import { env } from "@/env";
import { AppError } from "@/lib/errors";

const CSRF_TOKEN_PATH = "/auth/get-csrf-token/";
const CSRF_PROTECTED_METHODS = new Set(["post", "put", "patch", "delete"]);

type CSRFTokenResponse = {
  csrf_token?: string;
};

export abstract class APIService {
  protected baseURL: string;
  private axiosInstance: AxiosInstance;
  private header: Record<string, string> = {};
  private csrfToken?: string;
  private csrfTokenPromise?: Promise<string | undefined>;

  constructor(baseURL?: string) {
    this.baseURL = baseURL || env.API_BASE_URL;
    this.axiosInstance = axios.create({
      baseURL: this.baseURL,
      withCredentials: true,
      timeout: 20000,
    });
    this.setupInterceptors();
    this.setupCSRFInterceptor();
  }

  private setupInterceptors() {
    this.axiosInstance.interceptors.response.use(
      (response) => response,
      (error) => {
        return Promise.reject(new AppError(error));
      }
    );
  }

  private setupCSRFInterceptor() {
    this.axiosInstance.interceptors.request.use(async (config: InternalAxiosRequestConfig) => {
      if (!this.needsCSRFToken(config)) return config;

      const csrfToken = await this.getCSRFToken();
      if (csrfToken) {
        const headers = AxiosHeaders.from(config.headers);
        headers.set("X-CSRFTOKEN", csrfToken);
        if (this.header.Cookie) headers.set("Cookie", this.header.Cookie);
        config.headers = headers;
      }

      return config;
    });
  }

  private needsCSRFToken(config: InternalAxiosRequestConfig) {
    const method = config.method?.toLowerCase();
    if (!method || !CSRF_PROTECTED_METHODS.has(method)) return false;
    if (config.withCredentials === false) return false;

    const url = config.url ?? "";
    if (url.includes(CSRF_TOKEN_PATH)) return false;
    if (this.isExternalURL(url)) return false;

    const headers = AxiosHeaders.from(config.headers);
    return !(headers.has("X-CSRFTOKEN") || headers.has("X-CSRFToken") || headers.has("X-CSRF-TOKEN"));
  }

  private isExternalURL(url: string) {
    if (!/^https?:\/\//i.test(url)) return false;

    const baseURL = this.baseURL.replace(/\/$/, "");
    return !baseURL || !url.startsWith(baseURL);
  }

  private async getCSRFToken() {
    if (this.csrfToken) return this.csrfToken;

    if (!this.csrfTokenPromise) {
      const tokenURL = `${this.baseURL.replace(/\/$/, "")}${CSRF_TOKEN_PATH}`;
      this.csrfTokenPromise = axios
        .get<CSRFTokenResponse>(tokenURL, {
          headers: this.getHeader(),
          validateStatus: null,
        })
        .then((response) => {
          this.mergeSetCookieHeaders(response.headers["set-cookie"]);
          const csrfToken = response.data?.csrf_token;
          if (csrfToken) this.csrfToken = csrfToken;
          return csrfToken;
        })
        .finally(() => {
          this.csrfTokenPromise = undefined;
        });
    }

    return this.csrfTokenPromise;
  }

  private mergeSetCookieHeaders(setCookieHeaders?: string[] | string) {
    if (!setCookieHeaders) return;

    const cookies = new Map<string, string>();
    const currentCookieHeader = this.header.Cookie;
    if (currentCookieHeader) {
      for (const cookie of currentCookieHeader.split(";")) {
        const [name, ...valueParts] = cookie.trim().split("=");
        if (name && valueParts.length > 0) cookies.set(name, valueParts.join("="));
      }
    }

    const responseCookieHeaders = Array.isArray(setCookieHeaders) ? setCookieHeaders : [setCookieHeaders];
    for (const setCookieHeader of responseCookieHeaders) {
      const [cookie] = setCookieHeader.split(";");
      const [name, ...valueParts] = cookie.trim().split("=");
      if (name && valueParts.length > 0) cookies.set(name, valueParts.join("="));
    }

    this.header.Cookie = Array.from(cookies.entries())
      .map(([name, value]) => `${name}=${value}`)
      .join("; ");
  }

  setHeader(key: string, value: string) {
    this.header[key] = value;
  }

  getHeader() {
    return this.header;
  }

  get(url: string, params = {}, config = {}) {
    return this.axiosInstance.get(url, {
      ...params,
      ...config,
    });
  }

  post(url: string, data = {}, config = {}) {
    return this.axiosInstance.post(url, data, config);
  }

  put(url: string, data = {}, config = {}) {
    return this.axiosInstance.put(url, data, config);
  }

  patch(url: string, data = {}, config = {}) {
    return this.axiosInstance.patch(url, data, config);
  }

  delete(url: string, data?: Record<string, unknown> | null | string, config = {}) {
    return this.axiosInstance.delete(url, { data, ...config });
  }

  request(config = {}) {
    return this.axiosInstance(config);
  }
}
