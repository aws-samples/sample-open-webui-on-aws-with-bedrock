// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import '@cloudscape-design/global-styles/index.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { AuthProvider } from 'react-oidc-context';
import { WebStorageStateStore } from 'oidc-client-ts';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { ConfigContext, loadConfig } from './config';

const root = createRoot(document.getElementById('root')!);

loadConfig()
  .then((cfg) => {
    // Cognito user pools are OIDC-discoverable at the issuer URL; Managed
    // Login renders the sign-in UX. PKCE code flow, no client secret (D2).
    const oidcConfig = {
      authority: `https://cognito-idp.${cfg.region}.amazonaws.com/${cfg.userPoolId}`,
      client_id: cfg.clientId,
      redirect_uri: `${window.location.origin}/auth/callback`,
      response_type: 'code',
      scope: 'openid email profile',
      // sessionStorage: cleared when the tab closes; not shared cross-tab.
      userStore: new WebStorageStateStore({ store: window.sessionStorage }),
      automaticSilentRenew: true,
      onSigninCallback: () => {
        // strip ?code=&state= and land on the saved route
        const target = sessionStorage.getItem('postLoginRoute') || '/';
        sessionStorage.removeItem('postLoginRoute');
        window.history.replaceState({}, '', target);
      },
    };
    root.render(
      <StrictMode>
        <ConfigContext.Provider value={cfg}>
          <AuthProvider {...oidcConfig}>
            <BrowserRouter>
              <App />
            </BrowserRouter>
          </AuthProvider>
        </ConfigContext.Provider>
      </StrictMode>,
    );
  })
  .catch((err: Error) => {
    root.render(
      <div style={{ fontFamily: 'sans-serif', padding: 40 }}>
        <h2>Metering console could not start</h2>
        <p>Failed to load deployment configuration: {err.message}</p>
        <p>Redeploy the metering stack (`./deploy.sh --metering`) to restore /config.json.</p>
      </div>,
    );
  });
