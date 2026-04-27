/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { AxiosInstance, AxiosRequestConfig, InternalAxiosRequestConfig } from "axios";
import axios, { AxiosHeaders } from "axios";

const CSRF_TOKEN_PATH = "/auth/get-csrf-token/";
const CSRF_PROTECTED_METHODS = new Set(["post", "put", "patch", "delete"]);

type CSRFTokenResponse = {
  csrf_token?: string;
};

/**
 * Abstract base class for making HTTP requests using axios
 * @abstract
 */
export abstract class APIService {
  protected baseURL: string;
  private axiosInstance: AxiosInstance;
  private csrfToken?: string;
  private csrfTokenPromise?: Promise<string | undefined>;

  /**
   * Creates an instance of APIService
   * @param {string} baseURL - The base URL for all HTTP requests
   */
  constructor(baseURL: string) {
    this.baseURL = baseURL;
    this.axiosInstance = axios.create({
      baseURL,
      withCredentials: true,
    });

    this.setupCSRFInterceptor();
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

  /**
   * Makes a GET request to the specified URL
   * @param {string} url - The endpoint URL
   * @param {object} [params={}] - URL parameters
   * @param {AxiosRequestConfig} [config={}] - Additional axios configuration
   * @returns {Promise} Axios response promise
   */
  get(url: string, params = {}, config: AxiosRequestConfig = {}) {
    return this.axiosInstance.get(url, {
      ...params,
      ...config,
    });
  }

  /**
   * Makes a POST request to the specified URL
   * @param {string} url - The endpoint URL
   * @param {object} [data={}] - Request body data
   * @param {AxiosRequestConfig} [config={}] - Additional axios configuration
   * @returns {Promise} Axios response promise
   */
  post(url: string, data = {}, config: AxiosRequestConfig = {}) {
    return this.axiosInstance.post(url, data, config);
  }

  /**
   * Makes a PUT request to the specified URL
   * @param {string} url - The endpoint URL
   * @param {object} [data={}] - Request body data
   * @param {AxiosRequestConfig} [config={}] - Additional axios configuration
   * @returns {Promise} Axios response promise
   */
  put(url: string, data = {}, config: AxiosRequestConfig = {}) {
    return this.axiosInstance.put(url, data, config);
  }

  /**
   * Makes a PATCH request to the specified URL
   * @param {string} url - The endpoint URL
   * @param {object} [data={}] - Request body data
   * @param {AxiosRequestConfig} [config={}] - Additional axios configuration
   * @returns {Promise} Axios response promise
   */
  patch(url: string, data = {}, config: AxiosRequestConfig = {}) {
    return this.axiosInstance.patch(url, data, config);
  }

  /**
   * Makes a DELETE request to the specified URL
   * @param {string} url - The endpoint URL
   * @param {any} [data] - Request body data
   * @param {AxiosRequestConfig} [config={}] - Additional axios configuration
   * @returns {Promise} Axios response promise
   */
  delete(url: string, data?: any, config: AxiosRequestConfig = {}) {
    return this.axiosInstance.delete(url, { data, ...config });
  }

  /**
   * Makes a custom request with the provided configuration
   * @param {object} [config={}] - Axios request configuration
   * @returns {Promise} Axios response promise
   */
  request(config = {}) {
    return this.axiosInstance(config);
  }
}
