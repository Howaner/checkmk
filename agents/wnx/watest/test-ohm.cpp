// test-ohm.cpp
// and ends there.
//
#include "pch.h"

#include <time.h>

#include <chrono>
#include <filesystem>
#include <future>
#include <string_view>

#include "cfg.h"
#include "cfg_details.h"
#include "cma_core.h"
#include "common/cfg_info.h"
#include "providers/ohm.h"
#include "read_file.h"
#include "service_processor.h"

namespace cma::provider {  // to become friendly for wtools classes
TEST(SectionProviderOhm, Construction) {
    OhmProvider ohm(kOhm, ',');
    EXPECT_EQ(ohm.getUniqName(), cma::section::kOhm);
}

TEST(SectionProviderOhm, ReadData) {
    namespace fs = std::filesystem;
    using namespace xlog::internal;
    cma::srv::TheMiniProcess oprocess;

    wtools::KillProcess(L"Openhardwaremonitorcli.exe", 1);

    fs::path ohm_exe = GetOhmCliPath();

    ASSERT_TRUE(cma::tools::IsValidRegularFile(ohm_exe))
        << "not found " << ohm_exe.u8string()
        << " probably directories are not ready to test\n";

    auto ret = oprocess.start(ohm_exe.wstring());
    ASSERT_TRUE(ret);
    ::Sleep(1000);
    EXPECT_TRUE(oprocess.running());

    OhmProvider ohm(provider::kOhm, ',');

    if (cma::tools::win::IsElevated()) {
        std::string out;
        for (auto i = 0; i < 30; ++i) {
            out = ohm.generateContent(section::kUseEmbeddedName, true);
            if (!out.empty()) break;
            xlog::sendStringToStdio(".", Colors::kYellow);
            ::Sleep(500);
        }
        xlog::sendStringToStdio("\n", Colors::kYellow);
        EXPECT_TRUE(!out.empty()) << "Probably you have to clean ohm";
        if (!out.empty()) {
            // testing output
            auto table = cma::tools::SplitString(out, "\n");

            // section header:
            EXPECT_TRUE(table.size() > 2);
            EXPECT_EQ(table[0], "<<<openhardwaremonitor:sep(44)>>>");

            // table header:
            auto header = cma::tools::SplitString(table[1], ",");
            EXPECT_EQ(header.size(), 6);
            if (header.size() >= 6) {
                const char* expected_strings[] = {"Index",  "Name",
                                                  "Parent", "SensorType",
                                                  "Value",  "WMIStatus"};
                int index = 0;
                for (auto& str : expected_strings) {
                    EXPECT_EQ(str, header[index++]);
                }
            }

            // table body:
            for (size_t i = 2; i < table.size(); i++) {
                auto f_line = cma::tools::SplitString(table[i], ",");
                EXPECT_EQ(f_line.size(), 6);
            }
        }

    } else {
        XLOG::l(XLOG::kStdio)
            .w("No testing of OpenHardwareMonitor. Program must be elevated");
    }

    ret = oprocess.stop();
    EXPECT_FALSE(oprocess.running());
    EXPECT_TRUE(ret);
}

}  // namespace cma::provider

// START STOP testing
namespace cma::srv {

// simple foo to calc processes by names in the PC
int CalcOhmCount() {
    using namespace cma::tools;
    int count = 0;
    std::string ohm_name{cma::provider::kOpenHardwareMonitorCli};
    StringLower(ohm_name);

    wtools::ScanProcessList(
        [ohm_name, &count](const PROCESSENTRY32& entry) -> bool {
            std::string incoming_name = wtools::ConvertToUTF8(entry.szExeFile);
            StringLower(incoming_name);
            if (ohm_name == incoming_name) count++;
            return true;
        });
    return count;
}

TEST(SectionProviderOhm, DoubleStart) {
    using namespace cma::tools;
    if (!win::IsElevated()) {
        XLOG::l(XLOG::kStdio)
            .w("No testing of OpenHardwareMonitor. Program must be elevated");
        return;
    }
    auto ohm_path = cma::provider::GetOhmCliPath();
    ASSERT_TRUE(IsValidRegularFile(ohm_path));

    auto count = CalcOhmCount();
    if (count != 0) {
        XLOG::l(XLOG::kStdio)
            .w("OpenHardwareMonitor already started, TESTING IS NOT POSSIBLE");
        return;
    }

    {
        TheMiniProcess oprocess;
        oprocess.start(ohm_path);
        count = CalcOhmCount();
        EXPECT_EQ(count, 1);
        oprocess.start(ohm_path);
        count = CalcOhmCount();
        EXPECT_EQ(count, 1);
    }
    count = CalcOhmCount();
    EXPECT_EQ(count, 0) << "OHM is not killed";
}

TEST(SectionProviderOhm, StartStop) {
    namespace fs = std::filesystem;
    TheMiniProcess oprocess;
    EXPECT_EQ(oprocess.process_id_, 0);
    EXPECT_EQ(oprocess.process_handle_, INVALID_HANDLE_VALUE);
    EXPECT_EQ(oprocess.thread_handle_, INVALID_HANDLE_VALUE);

    // this approximate logic to find OHM executable
    fs::path ohm_exe = cma::cfg::GetUserDir();
    ohm_exe /= cma::cfg::dirs::kAgentBin;
    ohm_exe /= cma::provider::kOpenHardwareMonitorCli;
    // Now check this logic vs API
    EXPECT_EQ(cma::provider::GetOhmCliPath(), ohm_exe);
    // Presence
    ASSERT_TRUE(cma::tools::IsValidRegularFile(ohm_exe))
        << "not found " << ohm_exe.u8string()
        << " probably directories are not ready to test\n";

    auto ret = oprocess.start(ohm_exe.wstring());
    ASSERT_TRUE(ret);
    ::Sleep(500);
    EXPECT_TRUE(oprocess.running());

    ret = oprocess.stop();
    EXPECT_FALSE(oprocess.running());
    EXPECT_EQ(oprocess.process_id_, 0);
    EXPECT_EQ(oprocess.process_handle_, INVALID_HANDLE_VALUE);
    EXPECT_EQ(oprocess.thread_handle_, INVALID_HANDLE_VALUE);
    EXPECT_TRUE(ret);
}
}  // namespace cma::srv
