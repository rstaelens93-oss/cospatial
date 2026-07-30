import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.cospatial.app',
  appName: 'CoSpatial',
  webDir: 'dist/public',
  plugins: {
    SplashScreen: {
      launchShowDuration: 3000,
      launchAutoHide: true,
      backgroundColor: '#0b0f19'
    }
  }
};

export default config;
