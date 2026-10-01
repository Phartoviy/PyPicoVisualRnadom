/*
 * Квантовый скаттерплот — Raspberry Pi Pico / Pico W (RP2040).
 * Пакет плат: Arduino-Pico от Earle F. Philhower.
 * USB Stack: Pico SDK. Serial — встроенный USB CDC, не UART.
 * Источник: ROSC.RANDOMBIT; резерв: младший бит АЦП датчика температуры.
 * Название художественное: квантовая природа и криптостойкость не доказаны.
 */
#include <Arduino.h>
#include "hardware/adc.h"
#include "hardware/regs/rosc.h"
#include "hardware/structs/rosc.h"

#if !defined(ARDUINO_ARCH_RP2040) || !defined(PICO_RP2040) || !PICO_RP2040
#error "Select Raspberry Pi Pico / Pico W (RP2040), Earle Philhower Arduino-Pico core"
#endif

constexpr uint32_t RATE_HZ = 100;
constexpr bool FORCE_ADC = false;       // Для отдельной проверки резервного режима.
constexpr uint32_t SAMPLE_DELAY_US = 5;
constexpr uint32_t BYTE_TIMEOUT_US = 100000;
static_assert(RATE_HZ >= 1 && RATE_HZ <= 100, "RATE_HZ must be 1..100");
constexpr uint32_t PERIOD_US = (1000000UL + RATE_HZ - 1) / RATE_HZ;

bool useAdc = false;
bool wasConnected = false;
uint32_t lastSendUs = 0;

// Прототипы заданы явно, скетч не зависит от автогенерации Arduino IDE.
void selectAdc();
uint8_t readNoiseBit();
bool generateByte(uint8_t &value);
void reportSource();

void selectAdc() {
    // Читаем исходный 12-разрядный отсчет, без масштабирования analogRead.
    adc_init();
    adc_set_temp_sensor_enabled(true);
    adc_select_input(4);
    delay(1);  // Даем датчику установиться после включения.
    useAdc = true;
}

uint8_t readNoiseBit() {
    // Разносим выборки ROSC: не читаем регистр подряд с частотой шины.
    delayMicroseconds(SAMPLE_DELAY_US);
    if (useAdc) {
        return static_cast<uint8_t>(adc_read() & 1u);
    }
    return static_cast<uint8_t>(rosc_hw->randombit & 1u);
}

bool generateByte(uint8_t &value) {
    const uint32_t startedUs = micros();
    value = 0;
    uint8_t accepted = 0;
    while (accepted < 8) {
        // Вычитание unsigned корректно и при переполнении micros().
        if (static_cast<uint32_t>(micros() - startedUs) >= BYTE_TIMEOUT_US) {
            return false;
        }
        const uint8_t a = readNoiseBit();
        const uint8_t b = readNoiseBit();
        // Корректор фон Неймана: 00/11 пропускаем; 01 -> 0, 10 -> 1.
        // Уменьшает постоянное смещение независимых исходных битов.
        // Произвольную корреляцию и недостаточную энтропию не исправляет.
        if (a != b) {
            value = static_cast<uint8_t>((value << 1) | a);
            ++accepted;
        }
        yield(); // Обслуживание фоновых задач Arduino, в том числе USB.
    }
    return true;
}

void reportSource() {
    // ПК распознает строки с # как диагностику и не считает их числами.
    Serial.println(useAdc ? "# SOURCE ADC" : "# SOURCE ROSC");
}

void setup() {
    Serial.begin(115200);
    // Не меняем тактирование: оставьте штатную частоту платы в Arduino IDE.
    // Проверяем, включен и стабилен ли кольцевой генератор.
    const uint32_t mask = ROSC_STATUS_ENABLED_BITS | ROSC_STATUS_STABLE_BITS;
    if (FORCE_ADC || (rosc_hw->status & mask) != mask) {
        selectAdc();
    }
    // Не блокируем setup бесконечным while (!Serial).
    lastSendUs = micros();
}

void loop() {
    // Пока программа ПК не открыла USB-порт, не накапливаем вывод.
    if (!Serial) {
        wasConnected = false;
        delay(10);
        return;
    }
    if (!wasConnected) {
        wasConnected = true;
        reportSource();
        lastSendUs = micros();
    }

    // Не более 100 отправок в секунду, без догоняющих пачек после задержек.
    if (static_cast<uint32_t>(micros() - lastSendUs) < PERIOD_US) {
        delay(1);
        return;
    }

    uint8_t value = 0;
    if (!generateByte(value)) {
        if (!useAdc) {
            selectAdc();
            reportSource();
        } else {
            // Отсутствующую физическую энтропию не заменяем random().
            Serial.println("# ERROR ADC entropy unavailable");
            delay(1000);
        }
        return;
    }

    if (Serial) {
        // Явный числовой тип: передаем десятичное число, а не символ байта.
        Serial.println(static_cast<unsigned int>(value));
        lastSendUs = micros();
    }
}
