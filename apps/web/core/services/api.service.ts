/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */
import type { AxiosInstance, AxiosRequestConfig, InternalAxiosRequestConfig } from "axios";
import axios, { AxiosHeaders } from "axios";

const CSRF_TOKEN_PATH = "/auth/get-csrf-token/";
const CSRF_PROTECTED_METHODS = new Set(["post", "put", "patch", "delete"]);

type CSRFTokenResponse = {
  csrf_token?: string;
};

export abstract class APIService {
  protected baseURL: string;
  private axiosInstance: AxiosInstance;
  private csrfToken?: string;
  private csrfTokenPromise?: Promise<string | undefined>;

  constructor(baseURL: string) {
    this.baseURL = baseURL;
    this.axiosInstance = axios.create({
      baseURL,
      withCredentials: true,
    });

    this.setupInterceptors();
    this.setupCSRFInterceptor();
  }

  private setupInterceptors() {
    this.axiosInstance.interceptors.response.use(
      (response) => response,
      (error) => {
        if (error.response && error.response.status === 401) {
          const currentPath = window.location.pathname;
          window.location.replace(`/${currentPath ? `?next_path=${currentPath}` : ``}`);
        }
        return Promise.reject(error);
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
          withCredentials: true,
          validateStatus: null,
        })
        .then((response) => {
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

  get(url: string, params = {}, config: AxiosRequestConfig = {}) {
    return this.axiosInstance.get(url, {
      ...params,
      ...config,
    });
  }

  post(url: string, data = {}, config: AxiosRequestConfig = {}) {
    return this.axiosInstance.post(url, data, config);
  }

  put(url: string, data = {}, config: AxiosRequestConfig = {}) {
    return this.axiosInstance.put(url, data, config);
  }

  patch(url: string, data = {}, config: AxiosRequestConfig = {}) {
    return this.axiosInstance.patch(url, data, config);
  }

  delete(url: string, data?: any, config: AxiosRequestConfig = {}) {
    return this.axiosInstance.delete(url, { data, ...config });
  }

  request(config = {}) {
    return this.axiosInstance(config);
  }
}
