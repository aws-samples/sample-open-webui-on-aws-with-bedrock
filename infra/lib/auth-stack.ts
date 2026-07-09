// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';

export interface AuthStackProps extends cdk.StackProps {
  callbackUrls?: string[];
  logoutUrls?: string[];
  cognitoDomainPrefix?: string;
}

export class AuthStack extends cdk.Stack {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;
  public readonly userPoolDomain: cognito.UserPoolDomain;

  constructor(scope: Construct, id: string, props?: AuthStackProps) {
    super(scope, id, props);

    // Cognito User Pool
    this.userPool = new cognito.UserPool(this, 'OpenWebUIUserPool', {
      userPoolName: 'open-webui-users',
      selfSignUpEnabled: true,
      signInAliases: {
        email: true,
      },
      autoVerify: {
        email: true,
      },
      standardAttributes: {
        email: {
          required: true,
          mutable: true,
        },
        fullname: {
          required: false,
          mutable: true,
        },
      },
      passwordPolicy: {
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: false,
      },
      mfa: cognito.Mfa.OPTIONAL,
      mfaSecondFactor: {
        sms: false,
        otp: true,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // User Pool Client
    this.userPoolClient = new cognito.UserPoolClient(this, 'OpenWebUIClient', {
      userPool: this.userPool,
      userPoolClientName: 'open-webui-app',
      generateSecret: true,
      authFlows: {
        userSrp: true,
      },
      oAuth: {
        flows: {
          authorizationCodeGrant: true,
        },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
        ],
        callbackUrls: props?.callbackUrls || ['https://localhost/oauth/oidc/callback'],
        logoutUrls: props?.logoutUrls || ['https://localhost/auth'],
      },
      preventUserExistenceErrors: true,
    });

    // Cognito Domain for Managed Login
    this.userPoolDomain = new cognito.UserPoolDomain(this, 'OpenWebUIDomain', {
      userPool: this.userPool,
      cognitoDomain: {
        domainPrefix: props?.cognitoDomainPrefix ?? `open-webui-${cdk.Aws.ACCOUNT_ID}`,
      },
      managedLoginVersion: cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
    });

    // Managed Login branding (modern responsive UI, replaces classic Hosted UI)
    new cognito.CfnManagedLoginBranding(this, 'ManagedLoginBranding', {
      userPoolId: this.userPool.userPoolId,
      clientId: this.userPoolClient.userPoolClientId,
      useCognitoProvidedValues: true,
    });

    // User Pool Groups for role mapping
    new cognito.CfnUserPoolGroup(this, 'AdminGroup', {
      userPoolId: this.userPool.userPoolId,
      groupName: 'admin',
      description: 'Admin users with full access to all features and models',
    });

    new cognito.CfnUserPoolGroup(this, 'UserGroup', {
      userPoolId: this.userPool.userPoolId,
      groupName: 'user',
      description: 'Standard users',
    });

    new cognito.CfnUserPoolGroup(this, 'PowerUsersGroup', {
      userPoolId: this.userPool.userPoolId,
      groupName: 'power-users',
      description: 'Power users with access to advanced Bedrock models',
    });

    new cognito.CfnUserPoolGroup(this, 'BasicUsersGroup', {
      userPoolId: this.userPool.userPoolId,
      groupName: 'basic-users',
      description: 'Basic users with limited Bedrock model access',
    });

    // Outputs
    new cdk.CfnOutput(this, 'UserPoolId', {
      value: this.userPool.userPoolId,
      description: 'Cognito User Pool ID',
    });

    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
      description: 'Cognito User Pool Client ID',
    });

    new cdk.CfnOutput(this, 'CognitoDomain', {
      value: `${this.userPoolDomain.domainName}.auth.${cdk.Aws.REGION}.amazoncognito.com`,
      description: 'Cognito Hosted UI Domain',
    });
  }
}
