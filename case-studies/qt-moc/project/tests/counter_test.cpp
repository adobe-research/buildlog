// SPDX-FileCopyrightText: Copyright 2026 Adobe. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// Runs headless: QT_QPA_PLATFORM=offscreen (set below unless the
// environment says otherwise).
#include <QApplication>
#include <QFile>
#include <QLabel>
#include <QMetaProperty>
#include <QPushButton>
#include <cstdio>
#include <cstdlib>

#include "counter.h"
#include "version.h"

static int failures = 0;
#define CHECK(x)                                                    \
  do {                                                              \
    if (!(x)) {                                                     \
      std::fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, \
                   __LINE__, #x);                                   \
      ++failures;                                                   \
    }                                                               \
  } while (0)

int main(int argc, char **argv) {
  qputenv("QT_QPA_PLATFORM", qgetenv("QT_QPA_PLATFORM").isEmpty()
                                 ? QByteArray("offscreen")
                                 : qgetenv("QT_QPA_PLATFORM"));
  QApplication app(argc, argv);

  Counter counter;
  QLabel label;
  QPushButton button("+1");
  QObject::connect(&counter, &Counter::valueChanged, &label,
                   [&label](int v) { label.setNum(v); });
  QObject::connect(&button, &QPushButton::clicked, &counter,
                   &Counter::increment);
  button.click();
  button.click();
  CHECK(counter.value() == 2);
  CHECK(label.text() == "2");

  // String-based connection and the property system: moc's tables.
  Counter mirror;
  QObject::connect(&counter, SIGNAL(valueChanged(int)), &mirror,
                   SLOT(setValue(int)));
  counter.setProperty("value", 10);
  CHECK(mirror.value() == 10);
  CHECK(mirror.property("value").toInt() == 10);
  CHECK(counter.metaObject()->className() == QByteArray("Counter"));
  CHECK(counter.metaObject()->indexOfProperty("value") >= 0);

  // A resource from greeting.qrc (rcc), compiled into this executable.
  QFile greeting(":/greeting.txt");
  CHECK(greeting.open(QIODevice::ReadOnly));
  CHECK(greeting.readAll().trimmed() == "hello from rcc");
  CHECK(counter_version == 1);

  if (failures == 0)
    std::printf("counter_test: ok\n");
  return failures == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
