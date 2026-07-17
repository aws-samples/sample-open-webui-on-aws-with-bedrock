// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
// App shell: sign-in gate → module config probe (is_admin) → routed console.

import {
  AppLayout,
  Box,
  Button,
  Container,
  ContentLayout,
  Flashbar,
  Header,
  SideNavigation,
  Spinner,
  TopNavigation,
} from '@cloudscape-design/components';
import { applyMode, Mode } from '@cloudscape-design/global-styles';
import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import { useAuth } from 'react-oidc-context';
import { Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { api, ApiError, setTokenProvider } from './api';
import { cognitoSignOut, useConfig } from './config';
import type { ModuleConfig } from './types';
import AuditPage from './pages/AuditPage';
import DashboardPage from './pages/DashboardPage';
import GroupsPage from './pages/GroupsPage';
import HealthPage from './pages/HealthPage';
import MyUsagePage from './pages/MyUsagePage';
import PoliciesPage from './pages/PoliciesPage';
import PricingPage from './pages/PricingPage';
import UserDetailPage from './pages/UserDetailPage';
import UsersPage from './pages/UsersPage';

export const ModuleContext = createContext<ModuleConfig | null>(null);
export function useModule(): ModuleConfig {
  const m = useContext(ModuleContext);
  if (!m) throw new Error('module config not loaded');
  return m;
}

/** The caller's own sub — pages disable self-targeted mutations with it. */
export const SelfContext = createContext<string>('');

function CenteredCard(props: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'center', paddingTop: '18vh' }}>
      <div style={{ width: 460 }}>
        <Container header={<Header variant="h1">{props.title}</Header>}>{props.children}</Container>
      </div>
    </div>
  );
}

export default function App() {
  const auth = useAuth();
  const cfg = useConfig();
  const location = useLocation();
  const navigate = useNavigate();
  const [module, setModule] = useState<ModuleConfig | null>(null);
  const [moduleErr, setModuleErr] = useState<string | null>(null);
  const [dark, setDark] = useState<boolean>(() => window.matchMedia('(prefers-color-scheme: dark)').matches);

  useEffect(() => applyMode(dark ? Mode.Dark : Mode.Light), [dark]);
  // Access token carries cognito:groups — the claim the API authorizes on.
  useEffect(() => setTokenProvider(() => auth.user?.access_token), [auth.user]);

  useEffect(() => {
    if (!auth.isAuthenticated) return;
    api
      .get<ModuleConfig>('/config')
      .then(setModule)
      .catch((e: ApiError) => setModuleErr(e.message));
  }, [auth.isAuthenticated]);

  const email = (auth.user?.profile?.email as string) ?? '';
  const sub = (auth.user?.profile?.sub as string) ?? '';

  const navItems = useMemo(() => {
    if (!module?.is_admin) {
      return [{ type: 'link' as const, text: 'My usage', href: '/me' }];
    }
    return [
      { type: 'link' as const, text: 'Dashboard', href: '/' },
      { type: 'link' as const, text: 'Users', href: '/users' },
      { type: 'link' as const, text: 'Teams & groups', href: '/groups' },
      { type: 'link' as const, text: 'Quota policies', href: '/policies' },
      { type: 'link' as const, text: 'Model pricing', href: '/pricing' },
      { type: 'link' as const, text: 'Audit trail', href: '/audit' },
      { type: 'link' as const, text: 'Module health', href: '/health' },
      { type: 'divider' as const },
      { type: 'link' as const, text: 'My usage', href: '/me' },
    ];
  }, [module]);

  if (auth.isLoading) {
    return (
      <CenteredCard title="Metering Admin Console">
        <Box textAlign="center" padding="l">
          <Spinner size="large" /> <Box variant="span">Checking session…</Box>
        </Box>
      </CenteredCard>
    );
  }

  if (auth.error) {
    return (
      <CenteredCard title="Sign-in failed">
        <Box variant="p">{auth.error.message}</Box>
        <Button
          variant="primary"
          onClick={() => {
            sessionStorage.setItem('postLoginRoute', '/');
            void auth.signinRedirect();
          }}
        >
          Try again
        </Button>
      </CenteredCard>
    );
  }

  if (!auth.isAuthenticated) {
    return (
      <CenteredCard title="Metering Admin Console">
        <Box variant="p">
          Monitor LLM consumption, manage quotas, and investigate usage for your Open WebUI
          deployment. Sign in with your organization account — administrators are recognized by
          their existing admin group.
        </Box>
        <Button
          variant="primary"
          fullWidth
          onClick={() => {
            sessionStorage.setItem('postLoginRoute', location.pathname + location.search);
            void auth.signinRedirect();
          }}
        >
          Sign in
        </Button>
      </CenteredCard>
    );
  }

  if (moduleErr) {
    return (
      <CenteredCard title="Metering Admin Console">
        <Flashbar
          items={[
            {
              type: 'error',
              header: 'Could not reach the metering API',
              content: moduleErr,
              id: 'cfg-err',
            },
          ]}
        />
        <Box padding={{ top: 'm' }}>
          <Button onClick={() => window.location.reload()}>Retry</Button>{' '}
          <Button variant="link" onClick={() => cognitoSignOut(cfg)}>
            Sign out
          </Button>
        </Box>
      </CenteredCard>
    );
  }

  if (!module) {
    return (
      <CenteredCard title="Metering Admin Console">
        <Box textAlign="center" padding="l">
          <Spinner size="large" /> <Box variant="span">Loading module configuration…</Box>
        </Box>
      </CenteredCard>
    );
  }

  const homeHref = module.is_admin ? '/' : '/me';

  return (
    <ModuleContext.Provider value={module}>
      <SelfContext.Provider value={sub}>
        <div id="top-nav" style={{ position: 'sticky', top: 0, zIndex: 1002 }}>
          <TopNavigation
            identity={{
              href: homeHref,
              title: 'Metering Admin Console',
              onFollow: (e) => {
                e.preventDefault();
                navigate(homeHref);
              },
            }}
            utilities={[
              {
                type: 'button',
                text: module.enforce_mode === 'ENFORCE' ? 'Enforcing' : 'Observe mode',
                iconName: module.enforce_mode === 'ENFORCE' ? 'lock-private' : 'unlocked',
                disableUtilityCollapse: true,
              },
              {
                type: 'button',
                iconName: dark ? 'star-filled' : 'star',
                text: dark ? 'Dark' : 'Light',
                onClick: () => setDark((d) => !d),
              },
              {
                type: 'menu-dropdown',
                text: email || 'Account',
                description: module.is_admin ? 'Administrator' : 'User',
                iconName: 'user-profile',
                items: [{ id: 'signout', text: 'Sign out' }],
                onItemClick: ({ detail }) => {
                  if (detail.id === 'signout') {
                    void auth.removeUser().then(() => cognitoSignOut(cfg));
                  }
                },
              },
            ]}
          />
        </div>
        <AppLayout
          headerSelector="#top-nav"
          toolsHide
          navigation={
            <SideNavigation
              activeHref={location.pathname}
              items={navItems}
              onFollow={(e) => {
                if (!e.detail.external) {
                  e.preventDefault();
                  navigate(e.detail.href);
                }
              }}
            />
          }
          content={
            <Routes>
              {module.is_admin ? (
                <>
                  <Route path="/" element={<DashboardPage />} />
                  <Route path="/users" element={<UsersPage />} />
                  <Route path="/users/:sub" element={<UserDetailPage />} />
                  <Route path="/groups" element={<GroupsPage />} />
                  <Route path="/policies" element={<PoliciesPage />} />
                  <Route path="/pricing" element={<PricingPage />} />
                  <Route path="/audit" element={<AuditPage />} />
                  <Route path="/health" element={<HealthPage />} />
                  <Route path="/me" element={<MyUsagePage />} />
                  <Route path="*" element={<Navigate to="/" replace />} />
                </>
              ) : (
                <>
                  <Route path="/me" element={<MyUsagePage />} />
                  <Route
                    path="*"
                    element={
                      location.pathname === '/me' ? null : <NonAdminLanding />
                    }
                  />
                </>
              )}
            </Routes>
          }
        />
      </SelfContext.Provider>
    </ModuleContext.Provider>
  );
}

function NonAdminLanding() {
  const navigate = useNavigate();
  return (
    <ContentLayout header={<Header variant="h1">Access limited</Header>}>
      <Container>
        <Box variant="p">
          Administration requires membership in an admin group. You can still review your own AI
          usage and limits.
        </Box>
        <Button variant="primary" onClick={() => navigate('/me')}>
          View my usage
        </Button>
      </Container>
    </ContentLayout>
  );
}
