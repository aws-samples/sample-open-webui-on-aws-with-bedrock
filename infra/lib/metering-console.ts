// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';

export interface MeteringConsoleProps {
  /** The sample's existing Cognito pool (auth-stack) — the ONE identity system. */
  userPool: cognito.IUserPool;
  /** Managed Login domain host, e.g. open-webui-<acct>.auth.<region>.amazoncognito.com. */
  userPoolDomainName: string;
  /** The metering admin HTTP API this console fronts. */
  httpApi: apigwv2.HttpApi;
}

/**
 * Metering Admin Console — static SPA hosting + identity wiring
 * (docs/plans/metering-admin-console/01-DECISIONS.md).
 *
 * One CloudFront distribution: private S3 (OAC) serves the Cloudscape SPA;
 * /api/* routes to the admin HTTP API's dedicated "api" stage, making the
 * API same-origin with the app — no CORS anywhere (D1). A no-secret PKCE
 * app client on the EXISTING pool signs admins in via Managed Login (D2).
 */
export class MeteringConsole extends Construct {
  public readonly distribution: cloudfront.Distribution;
  public readonly client: cognito.UserPoolClient;
  public readonly consoleUrl: string;

  constructor(scope: Construct, id: string, props: MeteringConsoleProps) {
    super(scope, id);
    const stack = cdk.Stack.of(this);

    // Built SPA — produced by deploy.sh (npm ci && npm run build in console/).
    const distDir = path.join(__dirname, '..', '..', 'console', 'dist');
    if (!fs.existsSync(path.join(distDir, 'index.html'))) {
      throw new Error(
        'console/dist/index.html not found — build the admin console first: ' +
        '(cd console && npm ci && npm run build). deploy.sh --metering does this automatically.',
      );
    }

    const accessLogs = new s3.Bucket(this, 'AccessLogs', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      // ACLs required by CloudFront standard logging (log-delivery group)
      objectOwnership: s3.ObjectOwnership.BUCKET_OWNER_PREFERRED,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      lifecycleRules: [{ expiration: cdk.Duration.days(90) }],
    });

    const siteBucket = new s3.Bucket(this, 'Site', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // SPA deep-link routing WITHOUT distribution-wide errorResponses — a 404→
    // index.html error mapping would also rewrite /api/* JSON errors (D1).
    // Extensionless, non-API viewer paths rewrite to index.html here instead.
    const spaRewrite = new cloudfront.Function(this, 'SpaRewrite', {
      comment: 'Metering console: rewrite extensionless SPA routes to /index.html',
      runtime: cloudfront.FunctionRuntime.JS_2_0,
      code: cloudfront.FunctionCode.fromInline(`
function handler(event) {
  var request = event.request;
  var uri = request.uri;
  if (!uri.startsWith('/api/') && uri.indexOf('.') === -1) {
    request.uri = '/index.html';
  }
  return request;
}
`),
    });

    // Strict security headers for the app (D2 token-handling mitigations).
    // connect-src: same-origin API + the two Cognito endpoints the OIDC
    // client calls from the browser (discovery/JWKS + token/logout).
    const csp = [
      "default-src 'none'",
      "script-src 'self'",
      // style-src is 'self' only — no inline-style allowance. The Vite
      // production build emits Cloudscape's CSS as a linked stylesheet
      // (assets/index-*.css); the bundle creates no <style> nodes and writes no
      // style attributes, and the app's few React `style={{…}}` props go
      // through the CSSOM, which CSP does not govern. Cloudscape's runtime
      // style injection lives in @cloudscape-design/theming-runtime and is
      // reached only through the applyTheme() custom-theming API, which this
      // console does not use. If you ever adopt applyTheme, that code path
      // reads a nonce from <meta name="nonce"> — supply a per-response nonce
      // instead of relaxing this directive.
      "style-src 'self'",
      "img-src 'self' data:",
      "font-src 'self' data:",
      `connect-src 'self' https://cognito-idp.${stack.region}.amazonaws.com https://${props.userPoolDomainName}`,
      "base-uri 'self'",
      "form-action 'self'",
      "frame-ancestors 'none'",
      "object-src 'none'",
    ].join('; ');
    const securityHeaders = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
      comment: 'Metering console security headers',
      securityHeadersBehavior: {
        contentSecurityPolicy: { contentSecurityPolicy: csp, override: true },
        strictTransportSecurity: {
          accessControlMaxAge: cdk.Duration.days(365 * 2),
          includeSubdomains: true,
          override: true,
        },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: {
          referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN,
          override: true,
        },
      },
    });

    // Same-origin API: /api/* → the HTTP API's dedicated "api" stage. The
    // stage name doubles as the path prefix, so no URI rewriting is needed —
    // CloudFront forwards /api/users and the stage routes /users (D1). The
    // $default stage remains for scripts/set-quota.sh.
    new apigwv2.HttpStage(this, 'ApiStage', {
      httpApi: props.httpApi,
      stageName: 'api',
      autoDeploy: true,
    });
    const apiOrigin = new origins.HttpOrigin(
      `${props.httpApi.apiId}.execute-api.${stack.region}.${stack.urlSuffix}`,
      { protocolPolicy: cloudfront.OriginProtocolPolicy.HTTPS_ONLY },
    );

    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: 'Metering admin console (opt-in metering module)',
      defaultRootObject: 'index.html',
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      enableLogging: true,
      logBucket: accessLogs,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        responseHeadersPolicy: securityHeaders,
        functionAssociations: [
          { function: spaRewrite, eventType: cloudfront.FunctionEventType.VIEWER_REQUEST },
        ],
      },
      additionalBehaviors: {
        '/api/*': {
          origin: apiOrigin,
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          // Forwards Authorization + query strings; strips Host (execute-api
          // requires its own host for SigV4-less JWT routing).
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        },
      },
    });
    this.consoleUrl = `https://${this.distribution.distributionDomainName}`;

    // The console's own app client on the EXISTING pool: public (no secret),
    // authorization-code + PKCE, token revocation on, 60-min access tokens.
    // Membership in the module's admin groups — not the client — is what
    // grants admin capability (D2); this client only authenticates.
    this.client = new cognito.UserPoolClient(this, 'Client', {
      userPool: props.userPool,
      userPoolClientName: 'metering-console',
      generateSecret: false,
      preventUserExistenceErrors: true,
      enableTokenRevocation: true,
      accessTokenValidity: cdk.Duration.minutes(60),
      idTokenValidity: cdk.Duration.minutes(60),
      refreshTokenValidity: cdk.Duration.hours(8),
      // Hosted-UI (Managed Login) authorization-code + PKCE ONLY. No direct
      // auth flows (USER_SRP/USER_PASSWORD) — this public client never
      // authenticates outside the hosted redirect, so leaving them off keeps
      // its attack surface to the redirect flow (review L3). The headless
      // canary/CLI uses its own separate client for direct auth.
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
        callbackUrls: [`${this.consoleUrl}/auth/callback`],
        logoutUrls: [`${this.consoleUrl}/`],
      },
    });

    // The pool's domain runs Managed Login (newer version): every app client
    // needs a branding style assigned or its hosted pages return "login pages
    // unavailable". Reuse Cognito's provided style, like the OWUI client.
    new cognito.CfnManagedLoginBranding(this, 'ManagedLoginBranding', {
      userPoolId: props.userPool.userPoolId,
      clientId: this.client.userPoolClientId,
      useCognitoProvidedValues: true,
    });

    // Ship the built SPA + a deploy-time config the app fetches at boot —
    // pool/client ids never get baked into the bundle (hygiene: no ids in git).
    new s3deploy.BucketDeployment(this, 'Deploy', {
      destinationBucket: siteBucket,
      distribution: this.distribution,
      distributionPaths: ['/*'],
      sources: [
        s3deploy.Source.asset(distDir),
        s3deploy.Source.jsonData('config.json', {
          region: stack.region,
          userPoolId: props.userPool.userPoolId,
          clientId: this.client.userPoolClientId,
          cognitoDomain: props.userPoolDomainName,
          apiBase: '/api',
        }),
      ],
    });

    new cdk.CfnOutput(stack, 'ConsoleUrl', {
      value: this.consoleUrl,
      description: 'Metering admin console URL',
    });
    new cdk.CfnOutput(stack, 'ConsoleClientId', {
      value: this.client.userPoolClientId,
      description: 'Cognito app client used by the metering console (PKCE, no secret)',
    });
  }
}
