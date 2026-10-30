plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "dev.rogersquare.lookout"
    compileSdk = 34

    defaultConfig {
        applicationId = "dev.rogersquare.lookout"
        minSdk = 29
        targetSdk = 34
        versionCode = 1
        versionName = "0.1"
        buildConfigField("String", "HUB_URL", "\"http://10.0.0.127:8765\"")
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            // Private sideloaded app: signed with the local debug key (no Play distribution).
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        buildConfig = true
        compose = true
    }

    testOptions {
        unitTests.all { it.useJUnitPlatform() }
    }
}

kotlin {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    // Home screen: Jetpack Compose + androidx.tv (D-pad focus, tvOS-style focus scale)
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.foundation)
    implementation(libs.tv.material)
    implementation(libs.activity.compose)

    // Live camera video in the overlay (RTSP over TCP from the hub's go2rtc)
    implementation(libs.media3.exoplayer)
    implementation(libs.media3.exoplayer.rtsp)
    // Hub alert stream (Server-Sent Events)
    implementation(libs.okhttp)
    implementation(libs.okhttp.sse)

    // Android stubs org.json in local unit tests; use the real implementation there
    testImplementation("org.json:json:20240303")
    testImplementation(platform(libs.junit.bom))
    testImplementation(libs.junit.jupiter)
    testRuntimeOnly(libs.junit.platform.launcher)
}
