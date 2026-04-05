import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.superarchi.aitrader',
  appName: 'AITRADER',
  webDir: 'www',
  backgroundColor: '#0b1524',
  ios: {
    contentInset: 'always',
    limitsNavigationsToAppBoundDomains: false,
  },
};

export default config;
